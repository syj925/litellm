import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import pytest
import pytest_asyncio
from pydantic import SecretStr

from litellm.proxy.retail import api_jwt_service as service_module
from litellm.proxy.retail.api_jwt_config import ApiJwtConfig, ApiJwtFailure
from litellm.proxy.retail.api_jwt_provider import VerifiedApiJwt
from litellm.proxy.retail.api_jwt_service import (
    ApiJwtService,
    close_api_jwt_service,
    get_api_jwt_service,
    start_api_jwt_service,
)
from litellm.proxy.retail.api_jwt_store import ResolvedApiJwt
from litellm.proxy.retail.coordination_guard import CoordinationMonitor, GuardConfig

_FLAGS: Final = (
    "RETAIL_OPERATION_AUDIT_ENABLED", "RETAIL_NATIVE_AUDIT_ENABLED", "RETAIL_REQUIRE_COORDINATION_REDIS",
    "RETAIL_BILLING_ENABLED", "RETAIL_API_JWT_ENABLED",
)


def config() -> ApiJwtConfig:
    return ApiJwtConfig.model_validate({
        "namespace": "service-test", "generation": 1, "issuer": "https://issuer.test", "audience": "model-api",
        "client_id": "client", "jwks_uri": "https://issuer.test/keys",
        "bindings": [{"subject": "external", "user_id": "local", "token_hash": "a" * 64,
                      "binding_revision": "8af302bc-0926-4fbe-8dd9-bf2d681ed546"}],
    })


class Runtime:
    def __init__(self, *, close_error: bool = False) -> None:
        self.started = False
        self.closed = False
        self.close_error = close_error

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise RuntimeError("runtime cleanup fixture")

    def status(self) -> bool:
        return self.started and not self.closed

    async def verify(self, token: SecretStr) -> VerifiedApiJwt | ApiJwtFailure:
        return ApiJwtFailure("invalid")


class Store:
    def __init__(self, fail: str = "", *, close_error: bool = False) -> None:
        self.fail = fail
        self.calls: tuple[str, ...] = ()
        self.closed = False
        self.close_error = close_error

    async def initialize(self) -> ApiJwtFailure | None:
        self.calls = (*self.calls, "initialize")
        return ApiJwtFailure("unavailable") if self.fail == "initialize" else None

    async def configure(self, config: ApiJwtConfig) -> ApiJwtFailure | None:
        self.calls = (*self.calls, "configure")
        return ApiJwtFailure("invalid") if self.fail == "configure" else None

    async def fresh(self, config: ApiJwtConfig, verified: VerifiedApiJwt) -> ResolvedApiJwt | ApiJwtFailure:
        return ApiJwtFailure("invalid")

    async def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise RuntimeError("store cleanup fixture")


class Redis:
    def __init__(self, *, close_error: bool = False) -> None:
        self.closed = False
        self.close_error = close_error

    async def aclose(self, *, close_connection_pool: bool) -> None:
        self.closed = close_connection_pool
        if self.close_error:
            raise RuntimeError("redis cleanup fixture")


class Probe:
    def __init__(self) -> None:
        self.healthy = True
        self.now = 100.0

    async def ping(self) -> bool:
        return self.healthy

    async def recover(self) -> None:
        return None

    def clock(self) -> float:
        return self.now


@pytest_asyncio.fixture(autouse=True)
async def isolated_lifecycle(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setattr(service_module, "_LIFECYCLE", service_module._Lifecycle())
    yield
    await close_api_jwt_service()


def enable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _FLAGS:
        monkeypatch.setenv(name, "true")


def configured_file(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    path: Final = directory / "api-identity.json"
    path.write_text(config().model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("RETAIL_API_JWT_CONFIG", str(path))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("initialize", "configure"))
async def test_failed_authority_never_starts_verification_or_accepts_tokens(failure: str) -> None:
    runtime: Final = Runtime()
    store: Final = Store(failure)
    service: Final = ApiJwtService(config(), runtime, store, Redis())
    assert isinstance(await service.warm(), ApiJwtFailure)
    assert not service.ready and not runtime.started
    assert store.calls == (("initialize",) if failure == "initialize" else ("initialize", "configure"))


@pytest.mark.asyncio
async def test_successful_warm_and_close_keep_resource_ownership_separate() -> None:
    runtime: Final = Runtime()
    store: Final = Store()
    redis: Final = Redis()
    service: Final = ApiJwtService(config(), runtime, store, redis)
    assert await service.warm() is None
    assert service.ready and runtime.started and store.calls == ("initialize", "configure")
    await service.close()
    assert not service.ready and runtime.closed and store.closed and redis.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ("runtime", "store", "redis"))
async def test_cleanup_failure_still_closes_all_owned_resources_and_revokes_readiness(failed: str) -> None:
    runtime: Final = Runtime(close_error=failed == "runtime")
    store: Final = Store(close_error=failed == "store")
    redis: Final = Redis(close_error=failed == "redis")
    service: Final = ApiJwtService(config(), runtime, store, redis)
    assert await service.warm() is None
    with pytest.raises(RuntimeError, match=failed + " cleanup fixture"):
        await service.close()
    assert not service.ready and runtime.closed and store.closed and redis.closed


@pytest.mark.asyncio
async def test_valid_snapshot_cannot_accept_while_required_coordination_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("RETAIL_REQUIRE_COORDINATION_REDIS",):
        monkeypatch.setenv(name, "true")
    service: Final = ApiJwtService(config(), Runtime(), Store(), Redis())
    assert await service.warm() is None
    assert service.runtime.status()
    assert not service.available()
    await service.close()


@pytest.mark.asyncio
async def test_getter_rechecks_current_coordination_health_snapshot_and_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable(monkeypatch)
    probe: Final = Probe()
    monitor: Final = CoordinationMonitor(
        GuardConfig(required=True), probe=probe.ping, recover=probe.recover,
        clock=probe.clock, credential_headers=lambda: frozenset(),
    )
    monkeypatch.setattr(service_module, "coordination_guard_status", monitor.status)
    runtime: Final = Runtime()
    service: Final = ApiJwtService(config(), runtime, Store(), Redis())
    service_module._LIFECYCLE.service = service
    assert get_api_jwt_service() == ApiJwtFailure("unavailable")
    assert await service.warm() is None
    await monitor.check_once()
    assert get_api_jwt_service() is service
    probe.now += 3
    assert get_api_jwt_service() == ApiJwtFailure("unavailable")
    await monitor.check_once()
    assert get_api_jwt_service() is service
    probe.healthy = False
    await monitor.check_once()
    assert get_api_jwt_service() == ApiJwtFailure("unavailable")
    probe.healthy = True
    await monitor.check_once()
    assert get_api_jwt_service() is service
    await runtime.close()
    assert get_api_jwt_service() == ApiJwtFailure("unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", _FLAGS)
async def test_warmed_getter_denies_immediately_when_any_authority_dependency_is_disabled(
    monkeypatch: pytest.MonkeyPatch, disabled: str,
) -> None:
    enable(monkeypatch)
    monkeypatch.setattr(service_module, "coordination_guard_status", CoordinationMonitor(GuardConfig()).status)
    service: Final = ApiJwtService(config(), Runtime(), Store(), Redis())
    service_module._LIFECYCLE.service = service
    assert await service.warm() is None
    assert get_api_jwt_service() is service
    monkeypatch.setenv(disabled, "false")
    assert get_api_jwt_service() == ApiJwtFailure("unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", (None, "{}", "not-json"))
async def test_missing_or_invalid_optional_config_does_not_start_or_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configuration: str | None,
) -> None:
    enable(monkeypatch)
    if configuration is None:
        monkeypatch.delenv("RETAIL_API_JWT_CONFIG", raising=False)
    else:
        path: Final = tmp_path / "invalid-identity.json"
        path.write_text(configuration, encoding="utf-8")
        monkeypatch.setenv("RETAIL_API_JWT_CONFIG", str(path))
    await start_api_jwt_service()
    assert service_module._LIFECYCLE.task is None
    assert service_module._LIFECYCLE.service is None
    assert get_api_jwt_service() == ApiJwtFailure("unavailable")


@pytest.mark.asyncio
async def test_disabled_feature_has_no_startup_work_even_with_valid_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    enable(monkeypatch)
    monkeypatch.setenv("RETAIL_API_JWT_ENABLED", "false")
    configured_file(monkeypatch, tmp_path)
    await start_api_jwt_service()
    assert service_module._LIFECYCLE.task is None
    assert service_module._LIFECYCLE.service is None
    assert get_api_jwt_service() == ApiJwtFailure("unavailable")


@pytest.mark.asyncio
async def test_waiting_for_native_database_is_nonblocking_single_start_and_cancellable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from litellm.proxy import proxy_server

    enable(monkeypatch)
    configured_file(monkeypatch, tmp_path)
    monkeypatch.setattr(proxy_server, "prisma_client", None)
    await start_api_jwt_service()
    task: Final = service_module._LIFECYCLE.task
    assert task is not None and not task.done()
    await start_api_jwt_service()
    assert service_module._LIFECYCLE.task is task
    await asyncio.sleep(0)
    assert not task.done() and service_module._LIFECYCLE.service is None
    await close_api_jwt_service()
    assert task.cancelled()
    assert service_module._LIFECYCLE.task is None and service_module._LIFECYCLE.service is None
    assert get_api_jwt_service() == ApiJwtFailure("unavailable")


@pytest.mark.asyncio
async def test_optional_dependency_configuration_failure_is_deferred_and_private(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path,
) -> None:
    from litellm.proxy import proxy_server

    enable(monkeypatch)
    configured_file(monkeypatch, tmp_path)
    monkeypatch.setenv("REDIS_URL", "redis://fixture:fixture-private-password@localhost/0?socket_timeout=300")
    monkeypatch.setattr(proxy_server, "prisma_client", object())
    await start_api_jwt_service()
    task: Final = service_module._LIFECYCLE.task
    assert task is not None
    await asyncio.sleep(0)
    assert not task.done() and service_module._LIFECYCLE.service is None
    assert get_api_jwt_service() == ApiJwtFailure("unavailable")
    assert "Retail API access-token startup deferred (ValueError)" in caplog.text
    assert "fixture-private-password" not in caplog.text
    await close_api_jwt_service()
    assert task.cancelled()
