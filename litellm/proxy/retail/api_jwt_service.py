import asyncio
from contextlib import AsyncExitStack
from typing import Final, Protocol

from pydantic import SecretStr

from litellm._logging import verbose_proxy_logger

from .api_jwt_config import ApiJwtConfig, ApiJwtFailure, load_api_jwt_config
from .api_jwt_provider import VerifiedApiJwt
from .api_jwt_runtime import ApiJwtRuntime
from .api_jwt_store import NativeApiJwtStore, ResolvedApiJwt
from .control_plane_flags import api_jwt_enabled
from .coordination_guard import coordination_guard_status
from .native_audit_store import ensure_native_schema
from .oidc_service import dedicated_redis


class JwtVerifier(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    def status(self) -> bool: ...
    async def verify(self, token: SecretStr) -> VerifiedApiJwt | ApiJwtFailure: ...


class JwtAuthority(Protocol):
    async def initialize(self) -> ApiJwtFailure | None: ...
    async def configure(self, config: ApiJwtConfig) -> ApiJwtFailure | None: ...
    async def fresh(self, config: ApiJwtConfig, verified: VerifiedApiJwt) -> ResolvedApiJwt | ApiJwtFailure: ...
    async def close(self) -> None: ...


class RedisResource(Protocol):
    async def aclose(self, *, close_connection_pool: bool) -> None: ...


class ApiJwtService:
    def __init__(self, config: ApiJwtConfig, runtime: JwtVerifier, store: JwtAuthority, redis: RedisResource) -> None:
        self.config = config
        self.runtime = runtime
        self.store = store
        self.redis = redis
        self.ready = False

    async def warm(self) -> ApiJwtFailure | None:
        initialized: Final = await self.store.initialize()
        if isinstance(initialized, ApiJwtFailure):
            return initialized
        configured: Final = await self.store.configure(self.config)
        if isinstance(configured, ApiJwtFailure):
            return configured
        await self.runtime.start()
        self.ready = True
        return None

    def available(self) -> bool:
        return self.ready and coordination_guard_status().accepting_credentials and self.runtime.status()

    async def close(self) -> None:
        self.ready = False
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(self.redis.aclose, close_connection_pool=True)
            cleanup.push_async_callback(self.store.close)
            cleanup.push_async_callback(self.runtime.close)


class _Lifecycle:
    def __init__(self) -> None:
        self.service: ApiJwtService | None = None
        self.task: asyncio.Task[None] | None = None


_LIFECYCLE: Final = _Lifecycle()


async def _warm(config: ApiJwtConfig) -> None:
    from litellm.proxy import proxy_server

    while api_jwt_enabled():
        try:
            native: Final = proxy_server.prisma_client
            if native is None:
                await asyncio.sleep(5)
                continue
            if _LIFECYCLE.service is None:
                redis: Final = dedicated_redis()
                _LIFECYCLE.service = ApiJwtService(config, ApiJwtRuntime(config, redis), NativeApiJwtStore(native), redis)
            await ensure_native_schema(native)
            warmed: Final = await _LIFECYCLE.service.warm()
            if isinstance(warmed, ApiJwtFailure):
                await asyncio.sleep(5)
                continue
        except Exception as exc:  # noqa: BLE001  # optional access-token startup must not stop ordinary keys or settlement
            verbose_proxy_logger.warning("Retail API access-token startup deferred (%s)", type(exc).__name__)
        else:
            return
        await asyncio.sleep(5)


async def start_api_jwt_service() -> None:
    if not api_jwt_enabled() or _LIFECYCLE.task is not None:
        return
    config: Final = load_api_jwt_config()
    if isinstance(config, ApiJwtConfig):
        _LIFECYCLE.task = asyncio.create_task(_warm(config), name="retail-api-jwt-startup")


async def close_api_jwt_service() -> None:
    task: Final = _LIFECYCLE.task
    service: Final = _LIFECYCLE.service
    _LIFECYCLE.task = None
    _LIFECYCLE.service = None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if service is not None:
        await service.close()


def get_api_jwt_service() -> ApiJwtService | ApiJwtFailure:
    service: Final = _LIFECYCLE.service
    if not api_jwt_enabled() or service is None or not service.available():
        return ApiJwtFailure("unavailable")
    return service
