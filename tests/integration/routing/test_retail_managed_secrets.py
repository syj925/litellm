import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
import pytest
import pytest_asyncio
from openai import AsyncOpenAI, OpenAI
from pydantic import SecretStr

from litellm.proxy.retail.custom_secret_adapter import (
    ManagedCredentialAdapter,
    ManagedSecretUnavailable,
    managed_credential_kwargs,
)
from litellm.proxy.retail.secret_cache import ManagedSecretCache
from litellm.proxy.retail.secret_config import ManagedSecretsConfig, load_secret_config
from litellm.proxy.retail.secret_provider import SecretFailure, VaultKV2Provider


class VaultState:
    def __init__(self) -> None:
        self.value = "fixture-initial-key"
        self.version = 1
        self.status = 200
        self.location = ""
        self.hang = False
        self.release = asyncio.Event()
        self.requests: tuple[tuple[str, str, str], ...] = ()
        self.authorizations: tuple[str, ...] = ()
        self.reply_body: Mapping[str, object] | None = None

    def respond_with_status(self, status: int) -> None:
        self.status = status

    def rotate(self, value: str, version: int) -> None:
        self.value = value
        self.version = version

    def drip_response(self) -> None:
        self.hang = True

    def redirect_to(self, location: str) -> None:
        self.status = 307
        self.location = location

    def respond_with_body(self, body: Mapping[str, object]) -> None:
        self.reply_body = body

    async def respond(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head: Final = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            lines: Final = head.split("\r\n")
            headers: Final = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            self.requests = (*self.requests, (lines[0], headers.get("X-Vault-Token", ""),
                                             headers.get("X-Vault-Namespace", "")))
            self.authorizations = (*self.authorizations, headers.get("Authorization", ""))
            payload: Final = json.dumps(self.reply_body or {"data": {"data": {"api_key": self.value}, "metadata": {
                "version": self.version, "destroyed": False, "deletion_time": "",
            }}}).encode()
            content_length: Final = 10000 if self.hang else len(payload)
            redirect: Final = f"Location: {self.location}\r\n" if self.location else ""
            writer.write((f"HTTP/1.1 {self.status} Fixture\r\nContent-Length: {content_length}\r\n"
                          f"Content-Type: application/json\r\nConnection: close\r\n{redirect}\r\n").encode())
            await writer.drain()
            if self.hang:
                while not self.release.is_set():
                    writer.write(b" ")
                    await writer.drain()
                    await asyncio.sleep(0.04)
            else:
                writer.write(payload)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                return


@dataclass(frozen=True, slots=True)
class Vault:
    state: VaultState
    endpoint: str

    def config(self, changes: Mapping[str, object] | None = None) -> ManagedSecretsConfig:
        return ManagedSecretsConfig.model_validate({
            "endpoint": self.endpoint, "allow_loopback_http": True, "namespace": "fixture-namespace",
            "refs": [{"ref": "main", "path": "gateway/main"}], **(changes or {}),
        })


@pytest_asyncio.fixture(loop_scope="function")
async def vault() -> AsyncIterator[Vault]:
    state: Final = VaultState()
    server: Final = await asyncio.start_server(state.respond, "127.0.0.1", 0)
    port: Final = server.sockets[0].getsockname()[1]
    try:
        yield Vault(state, f"http://127.0.0.1:{port}")
    finally:
        state.release.set()
        server.close()
        await server.wait_closed()


async def until(predicate: Callable[[], bool], *, seconds: float = 4) -> None:
    deadline: Final = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("Managed credential state did not converge")


@pytest.mark.asyncio
async def test_real_kvv2_refresh_rotates_memory_reads_without_mutating_deployment(vault: Vault) -> None:
    settings: Final = vault.config()
    provider: Final = VaultKV2Provider(settings, SecretStr("fixture-auth-token"), pause=asyncio.sleep)
    subject: Final = ManagedSecretCache(settings, provider, clock=time.monotonic)
    adapter: Final = ManagedCredentialAdapter(subject)
    deployment: Final = {"litellm_params": {
        "model": "openai/fixture-model", "api_key": "retail-secret/main", "api_base": "https://upstream.test/v1",
    }}
    source_kwargs: Final = {"messages": [{"role": "user", "content": "fixture"}]}
    try:
        await subject.refresh()
        assert managed_credential_kwargs(deployment, source_kwargs, adapter=adapter) == {
            "api_key": "fixture-initial-key", "api_base": "https://upstream.test/v1",
        }
        assert tuple(adapter.read("main") for _ in range(100)) == ("fixture-initial-key",) * 100
        assert await asyncio.gather(*(adapter.async_read("main") for _ in range(100))) == ["fixture-initial-key"] * 100
        assert vault.state.requests == (("GET /v1/secret/data/gateway/main HTTP/1.1",
                                         "fixture-auth-token", "fixture-namespace"),)
        vault.state.rotate("fixture-rotated-key", 2)
        await subject.refresh()
        assert adapter.read("main") == await adapter.async_read("main") == "fixture-rotated-key"
        assert deployment["litellm_params"]["api_key"] == "retail-secret/main"
        assert source_kwargs == {"messages": [{"role": "user", "content": "fixture"}]}
        assert len(vault.state.requests) == 2
    finally:
        await subject.close()


@pytest.mark.asyncio
async def test_background_prewarm_recovers_then_outage_expires_and_revocation_closes_immediately(vault: Vault) -> None:
    settings: Final = vault.config({"ttl_seconds": 1, "refresh_seconds": 0.1, "timeout_seconds": 0.1, "retries": 0})
    vault.state.respond_with_status(503)
    provider: Final = VaultKV2Provider(settings, SecretStr("fixture-auth-token"), pause=asyncio.sleep)
    subject: Final = ManagedSecretCache(settings, provider, clock=time.monotonic)
    adapter: Final = ManagedCredentialAdapter(subject)
    try:
        await subject.start()
        assert subject.read("main") is None
        await until(lambda: bool(vault.state.requests))
        vault.state.respond_with_status(200)
        await until(lambda: subject.read("main") is not None)
        assert adapter.read("main") == "fixture-initial-key"
        vault.state.respond_with_status(503)
        await until(lambda: subject.read("main") is None)
        with pytest.raises(ManagedSecretUnavailable):
            adapter.read("main")
        vault.state.respond_with_status(200)
        vault.state.rotate("fixture-recovered-key", 2)
        await until(lambda: subject.read("main") is not None)
        assert adapter.read("main") == "fixture-recovered-key"
        vault.state.respond_with_status(403)
        await subject.refresh()
        assert subject.read("main") is None
    finally:
        await subject.close()


@pytest.mark.asyncio
async def test_total_read_deadline_bounds_a_provider_that_never_finishes_response_body(vault: Vault) -> None:
    settings: Final = vault.config({"timeout_seconds": 0.1, "retries": 1})
    vault.state.drip_response()
    provider: Final = VaultKV2Provider(settings, SecretStr("fixture-auth-token"), pause=asyncio.sleep)
    started: Final = time.monotonic()
    try:
        assert await asyncio.wait_for(provider.read(settings.refs[0]), timeout=0.8) == SecretFailure("unavailable")
        assert time.monotonic() - started < 1
        assert len(vault.state.requests) == settings.retries + 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_real_provider_redirect_is_not_followed_to_another_service(vault: Vault) -> None:
    foreign: Final = VaultState()
    foreign_server: Final = await asyncio.start_server(foreign.respond, "127.0.0.1", 0)
    vault.state.redirect_to(f"http://127.0.0.1:{foreign_server.sockets[0].getsockname()[1]}/capture")
    settings: Final = vault.config()
    provider: Final = VaultKV2Provider(settings, SecretStr("fixture-auth-token"), pause=asyncio.sleep)
    try:
        assert await provider.read(settings.refs[0]) == SecretFailure("invalid")
        assert len(vault.state.requests) == 1
        assert foreign.requests == ()
    finally:
        await provider.close()
        foreign_server.close()
        await foreign_server.wait_closed()


@pytest.mark.asyncio
async def test_vault_debug_logs_hide_provider_details_without_suppressing_concurrent_http(vault: Vault,
                                                                                       caplog: pytest.LogCaptureFixture
                                                                                       ) -> None:
    ordinary: Final = VaultState()
    ordinary_server: Final = await asyncio.start_server(ordinary.respond, "127.0.0.1", 0)
    ordinary_url: Final = f"http://127.0.0.1:{ordinary_server.sockets[0].getsockname()[1]}/ordinary-public"
    settings: Final = vault.config()
    provider: Final = VaultKV2Provider(settings, SecretStr("fixture-auth-token"), pause=asyncio.sleep)
    try:
        with caplog.at_level(logging.DEBUG), caplog.at_level(logging.DEBUG, logger="httpx"):
            async with httpx.AsyncClient(trust_env=False) as client:
                result: Final = await asyncio.gather(provider.read(settings.refs[0]), client.get(ordinary_url))
        assert result[0] != SecretFailure("unavailable")
        assert ordinary_url in caplog.text
        assert vault.endpoint not in caplog.text
        assert "fixture-auth-token" not in caplog.text
        assert "fixture-initial-key" not in caplog.text
        assert "gateway/main" not in caplog.text
    finally:
        await provider.close()
        ordinary_server.close()
        await ordinary_server.wait_closed()


def test_configuration_file_errors_do_not_echo_private_values(tmp_path: Path) -> None:
    file_path: Final = tmp_path / "managed-secrets.json"
    file_path.write_text('{"endpoint":"private-fixture-url","token":"private-fixture-token"}', encoding="utf-8")
    with pytest.raises(ValueError) as failure:
        load_secret_config({"RETAIL_MANAGED_SECRETS_ENABLED": "true", "RETAIL_MANAGED_SECRETS_CONFIG": str(file_path)})
    assert str(failure.value) == "Managed secret configuration is invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", (False, True))
async def test_openai_sdk_cross_origin_redirect_strips_managed_authorization(vault: Vault,
                                                                          asynchronous: bool) -> None:
    foreign: Final = VaultState()
    foreign.respond_with_body({"id": "fixture-response", "object": "chat.completion", "created": 1,
                          "model": "fixture-model", "choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": "fixture-success"}}]})
    server: Final = await asyncio.start_server(foreign.respond, "127.0.0.1", 0)
    vault.state.redirect_to(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/redirected")

    def call_sync() -> str | None:
        with OpenAI(api_key="fixture-managed-authorization", base_url=vault.endpoint, max_retries=0) as client:
            return client.chat.completions.create(model="fixture-model", messages=[]).choices[0].message.content

    try:
        if asynchronous:
            async with AsyncOpenAI(api_key="fixture-managed-authorization", base_url=vault.endpoint,
                                   max_retries=0) as client:
                response: Final = await client.chat.completions.create(model="fixture-model", messages=[])
                assert response.choices[0].message.content == "fixture-success"
        else:
            assert await asyncio.to_thread(call_sync) == "fixture-success"
        assert vault.state.authorizations == ("Bearer fixture-managed-authorization",)
        assert foreign.authorizations == ("",)
        assert len(foreign.requests) == 1
    finally:
        server.close()
        await server.wait_closed()
