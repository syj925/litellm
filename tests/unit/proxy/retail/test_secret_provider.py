from collections.abc import Mapping
from typing import Final

import httpx
import pytest
from pydantic import SecretStr

from litellm.proxy.retail.secret_config import ManagedSecretsConfig, SecretBinding
from litellm.proxy.retail.secret_provider import SecretFailure, SecretValue, VaultKV2Provider


async def no_pause(seconds: float) -> None:
    return None


def config(changes: Mapping[str, object] | None = None) -> ManagedSecretsConfig:
    return ManagedSecretsConfig.model_validate(
        {"endpoint": "https://vault.test", "refs": [{"ref": "main", "path": "models/main"}], **(changes or {})}
    )


def envelope(value: object = "fixture-key", metadata: Mapping[str, object] | None = None) -> Mapping[str, object]:
    return {"data": {"data": {"api_key": value}, "metadata": {"version": 1, **(metadata or {})}}}


@pytest.mark.asyncio
async def test_vault_read_uses_fixed_kvv2_path_token_namespace_and_version() -> None:
    settings: Final = config({"namespace": "business"})

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://vault.test/v1/secret/data/models/main"
        assert request.headers["X-Vault-Token"] == "fixture-auth"
        assert request.headers["X-Vault-Namespace"] == "business"
        return httpx.Response(200, json=envelope(metadata={"version": 9}))

    provider: Final = VaultKV2Provider(
        settings, SecretStr("fixture-auth"), pause=no_pause, transport=httpx.MockTransport(respond)
    )
    assert await provider.read(settings.refs[0]) == SecretValue(SecretStr("fixture-key"), 9)
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (401, 403, 404, 410))
async def test_provider_denials_are_revocation_without_response_body_leakage(status: int) -> None:
    settings: Final = config()
    provider: Final = VaultKV2Provider(
        settings, SecretStr("fixture-auth"), pause=no_pause,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text="private-fixture-error")),
    )
    result: Final = await provider.read(settings.refs[0])
    assert result == SecretFailure("revoked")
    assert "private-fixture" not in repr(result)
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,expected",
    (
        (envelope(metadata={"destroyed": True}), "revoked"),
        (envelope(metadata={"deletion_time": "2025-01-01T00:00:00Z"}), "revoked"),
        (envelope(""), "invalid"),
        (envelope("credential\nheader"), "invalid"),
        (envelope(123), "invalid"),
        (envelope(metadata={"version": 0}), "invalid"),
        (envelope(metadata={"version": True}), "invalid"),
        ({"private-fixture-error": "not-a-secret-response"}, "invalid"),
    ),
)
async def test_invalid_or_deleted_kvv2_data_never_becomes_a_credential(body: Mapping[str, object], expected: str) -> None:
    settings: Final = config()
    provider: Final = VaultKV2Provider(
        settings, SecretStr("fixture-auth"), pause=no_pause,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    assert await provider.read(settings.refs[0]) == SecretFailure("revoked" if expected == "revoked" else "invalid")
    await provider.close()


@pytest.mark.asyncio
async def test_provider_retries_are_bounded_and_unlisted_refs_never_issue_http() -> None:
    settings: Final = config({"retries": 2})
    calls: Final = Requests()

    def respond(request: httpx.Request) -> httpx.Response:
        calls.record(request)
        return httpx.Response(503, text="private-fixture-error")

    provider: Final = VaultKV2Provider(
        settings, SecretStr("fixture-auth"), pause=no_pause, transport=httpx.MockTransport(respond)
    )
    assert await provider.read(settings.refs[0]) == SecretFailure("unavailable")
    assert len(calls.paths) == settings.retries + 1
    assert await provider.read(SecretBinding(ref="other", path="private/other")) == SecretFailure("invalid")
    assert len(calls.paths) == settings.retries + 1
    await provider.close()


@pytest.mark.asyncio
async def test_redirect_and_oversized_response_fail_closed_without_following() -> None:
    settings: Final = config({"max_response_bytes": 1024})
    calls: Final = Requests()

    def respond(request: httpx.Request) -> httpx.Response:
        calls.record(request)
        return httpx.Response(302, headers={"Location": "https://foreign.test/private"})

    redirect: Final = VaultKV2Provider(
        settings, SecretStr("fixture-auth"), pause=no_pause, transport=httpx.MockTransport(respond)
    )
    assert await redirect.read(settings.refs[0]) == SecretFailure("invalid")
    assert len(calls.paths) == 1
    await redirect.close()
    oversized: Final = VaultKV2Provider(
        settings, SecretStr("fixture-auth"), pause=no_pause,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=envelope("x" * 1100))),
    )
    assert await oversized.read(settings.refs[0]) == SecretFailure("invalid")
    await oversized.close()


class Requests:
    def __init__(self) -> None:
        self.paths: tuple[str, ...] = ()

    def record(self, request: httpx.Request) -> None:
        self.paths = (*self.paths, str(request.url))
