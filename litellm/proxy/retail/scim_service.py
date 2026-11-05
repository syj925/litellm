import asyncio
from typing import Final, Protocol
from uuid import UUID

from litellm._logging import verbose_proxy_logger

from .api_jwt_auth import api_jwt_compatible
from .control_plane_flags import scim_enabled
from .coordination_guard import coordination_guard_status
from .native_audit_store import ensure_native_schema
from .scim_config import ScimSetup, load_scim_setup
from .scim_protocol import ScimFailure, ScimList, ScimProfile, ScimQuery, ScimRecord
from .scim_store import NativeScimStore


class ScimAuthority(Protocol):
    async def initialize(self) -> ScimFailure | None: ...
    async def configure(self, setup: ScimSetup) -> ScimFailure | None: ...
    async def list_users(self, setup: ScimSetup, query: ScimQuery) -> ScimList | ScimFailure: ...
    async def get_user(self, setup: ScimSetup, identifier: UUID) -> ScimRecord | ScimFailure: ...
    async def create_user(self, setup: ScimSetup, profile: ScimProfile) -> ScimRecord | ScimFailure: ...
    async def update_user(
        self, setup: ScimSetup, identifier: UUID, body: bytes, *, replace: bool, if_match: str | None,
    ) -> ScimRecord | ScimFailure: ...
    async def delete_user(self, setup: ScimSetup, identifier: UUID, if_match: str | None) -> ScimFailure | None: ...
    async def close(self) -> None: ...


class ScimService:
    def __init__(self, setup: ScimSetup, store: ScimAuthority) -> None:
        self.setup = setup
        self.store = store
        self.ready = False

    async def warm(self) -> ScimFailure | None:
        self.ready = False
        initialized: Final = await self.store.initialize()
        if isinstance(initialized, ScimFailure):
            return initialized
        configured: Final = await self.store.configure(self.setup)
        if isinstance(configured, ScimFailure):
            return configured
        self.ready = True
        return None

    async def close(self) -> None:
        self.ready = False
        await self.store.close()


class _Lifecycle:
    def __init__(self) -> None:
        self.service: ScimService | None = None
        self.task: asyncio.Task[None] | None = None


_LIFECYCLE: Final = _Lifecycle()


async def _warm(setup: ScimSetup) -> None:
    from litellm.proxy import proxy_server

    while scim_enabled():
        try:
            native: Final = proxy_server.prisma_client
            if native is None:
                await asyncio.sleep(5)
                continue
            if _LIFECYCLE.service is None:
                _LIFECYCLE.service = ScimService(setup, NativeScimStore(native))
            await ensure_native_schema(native)
            failure: Final = await _LIFECYCLE.service.warm()
            if isinstance(failure, ScimFailure):
                await asyncio.sleep(5)
                continue
        except Exception as exc:  # noqa: BLE001  # optional directory initialization must not stop inference or settlement
            verbose_proxy_logger.warning("Retail directory startup deferred (%s)", type(exc).__name__)
        else:
            return
        await asyncio.sleep(5)


async def start_scim_service() -> None:
    if not scim_enabled() or _LIFECYCLE.task is not None:
        return
    setup: Final = load_scim_setup()
    if isinstance(setup, ScimSetup):
        _LIFECYCLE.task = asyncio.create_task(_warm(setup), name="retail-scim-startup")


async def close_scim_service() -> None:
    task: Final = _LIFECYCLE.task
    service: Final = _LIFECYCLE.service
    _LIFECYCLE.task = None
    _LIFECYCLE.service = None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if service is not None:
        await service.close()


def get_scim_service() -> ScimService | ScimFailure:
    service: Final = _LIFECYCLE.service
    coordination: Final = coordination_guard_status()
    if (
        not scim_enabled() or service is None or not service.ready or not api_jwt_compatible()
        or not coordination.required or not coordination.accepting_credentials
    ):
        return ScimFailure(503)
    return service
