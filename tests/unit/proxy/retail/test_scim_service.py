from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr

from litellm.proxy.retail import scim_service as service_module
from litellm.proxy.retail.scim_config import ScimConfig, ScimSetup
from litellm.proxy.retail.scim_protocol import ScimFailure, ScimList, ScimProfile, ScimQuery, ScimRecord
from litellm.proxy.retail.scim_service import ScimService, close_scim_service, get_scim_service, start_scim_service

_FLAGS: Final = (
    "RETAIL_OPERATION_AUDIT_ENABLED", "RETAIL_NATIVE_AUDIT_ENABLED", "RETAIL_REQUIRE_COORDINATION_REDIS",
    "RETAIL_BILLING_ENABLED", "RETAIL_SCIM_ENABLED",
)


class Authority:
    def __init__(self, failure: Literal["initialize", "configure"] | None = None, *, close_error: bool = False) -> None:
        self.failure = failure
        self.close_error = close_error
        self.calls: tuple[str, ...] = ()
        self.closed = False

    async def initialize(self) -> ScimFailure | None:
        self.calls = (*self.calls, "initialize")
        return ScimFailure(503) if self.failure == "initialize" else None

    async def configure(self, setup: ScimSetup) -> ScimFailure | None:
        self.calls = (*self.calls, "configure")
        return ScimFailure(409) if self.failure == "configure" else None

    async def list_users(self, setup: ScimSetup, query: ScimQuery) -> ScimList | ScimFailure:
        return ScimFailure(503)

    async def get_user(self, setup: ScimSetup, identifier: UUID) -> ScimRecord | ScimFailure:
        return ScimFailure(503)

    async def create_user(self, setup: ScimSetup, profile: ScimProfile) -> ScimRecord | ScimFailure:
        return ScimFailure(503)

    async def update_user(
        self, setup: ScimSetup, identifier: UUID, body: bytes, *, replace: bool, if_match: str | None,
    ) -> ScimRecord | ScimFailure:
        return ScimFailure(503)

    async def delete_user(self, setup: ScimSetup, identifier: UUID, if_match: str | None) -> ScimFailure | None:
        return ScimFailure(503)

    async def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise RuntimeError("fixture authority cleanup failure")


def setup() -> ScimSetup:
    return ScimSetup(ScimConfig(namespace="service-directory", generation=1, public_origin="https://gateway.invalid"),
        SecretStr("rt-scim."+"f"*32))


def enable(monkeypatch: pytest.MonkeyPatch) -> None:
    for flag in _FLAGS:
        monkeypatch.setenv(flag, "true")


def configured(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    configuration: Final = setup()
    path: Final = directory / "directory.json"
    path.write_text(configuration.config.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("RETAIL_SCIM_CONFIG", str(path))
    monkeypatch.setenv("RETAIL_SCIM_TOKEN", configuration.token.get_secret_value())


@pytest_asyncio.fixture(autouse=True)
async def isolated_lifecycle() -> AsyncIterator[None]:
    await close_scim_service()
    yield
    await close_scim_service()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("initialize", "configure"))
async def test_authority_failure_never_marks_ready_and_can_retry_same_borrowed_port(failure: Literal["initialize", "configure"]) -> None:
    authority: Final = Authority(failure)
    service: Final = ScimService(setup(), authority)
    assert await service.warm() == ScimFailure(503 if failure == "initialize" else 409)
    assert not service.ready and not authority.closed
    assert authority.calls == (("initialize",) if failure == "initialize" else ("initialize", "configure"))
    authority.failure = None
    assert await service.warm() is None and service.ready
    assert authority.calls[-2:] == ("initialize", "configure")
    await service.close()
    assert not service.ready and authority.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("initialize", "configure"))
async def test_failed_rewarm_revokes_previously_successful_readiness(failure: Literal["initialize", "configure"]) -> None:
    authority: Final = Authority()
    service: Final = ScimService(setup(), authority)
    assert await service.warm() is None and service.ready
    authority.failure = failure
    assert isinstance(await service.warm(), ScimFailure)
    assert not service.ready


@pytest.mark.asyncio
async def test_cleanup_error_still_revokes_service_and_global_lifecycle_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_SCIM_ENABLED", "false")
    authority: Final = Authority(close_error=True)
    service: Final = ScimService(setup(), authority)
    assert await service.warm() is None
    service_module._LIFECYCLE.service = service
    with pytest.raises(RuntimeError, match="fixture authority cleanup failure"):
        await close_scim_service()
    assert authority.closed and not service.ready
    assert service_module._LIFECYCLE.service is None and service_module._LIFECYCLE.task is None
    assert get_scim_service() == ScimFailure(503)


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", _FLAGS)
async def test_any_disabled_requirement_starts_no_schema_task_even_with_valid_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, disabled: str,
) -> None:
    enable(monkeypatch)
    configured(monkeypatch, tmp_path)
    monkeypatch.setenv(disabled, "false")
    await start_scim_service()
    assert service_module._LIFECYCLE.service is None and service_module._LIFECYCLE.task is None
    assert get_scim_service() == ScimFailure(503)


@pytest.mark.asyncio
async def test_unset_optional_feature_defaults_to_no_service_or_startup_work(monkeypatch: pytest.MonkeyPatch) -> None:
    for flag in _FLAGS:
        monkeypatch.delenv(flag, raising=False)
    await start_scim_service()
    assert service_module._LIFECYCLE.service is None and service_module._LIFECYCLE.task is None
    assert get_scim_service() == ScimFailure(503)


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", (None, "{}", "not-json"))
async def test_invalid_optional_configuration_never_starts_background_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configuration: str | None,
) -> None:
    enable(monkeypatch)
    configured(monkeypatch, tmp_path)
    if configuration is None:
        monkeypatch.delenv("RETAIL_SCIM_CONFIG", raising=False)
    else:
        path: Final = tmp_path / "invalid-directory.json"
        path.write_text(configuration, encoding="utf-8")
        monkeypatch.setenv("RETAIL_SCIM_CONFIG", str(path))
    await start_scim_service()
    assert service_module._LIFECYCLE.service is None and service_module._LIFECYCLE.task is None
    assert get_scim_service() == ScimFailure(503)


@pytest.mark.asyncio
async def test_optional_start_is_single_background_task_and_shutdown_cancels_before_native_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    enable(monkeypatch)
    configured(monkeypatch, tmp_path)
    await start_scim_service()
    task: Final = service_module._LIFECYCLE.task
    assert task is not None and not task.done()
    await start_scim_service()
    assert service_module._LIFECYCLE.task is task and service_module._LIFECYCLE.service is None
    await close_scim_service()
    assert task.cancelled()
    assert service_module._LIFECYCLE.task is None and service_module._LIFECYCLE.service is None
