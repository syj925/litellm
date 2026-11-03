import asyncio
import math
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final, Literal, Protocol
from urllib.parse import unquote_to_bytes

from pydantic import BaseModel, ConfigDict
from starlette.types import ASGIApp, Receive, Scope, Send

from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.db.routing_prisma_wrapper import RoutingPrismaWrapper
from litellm.types.proxy.auth.special_headers import SpecialHeaders

_CREDENTIAL_HEADERS: Final = frozenset(name.encode() for name in SpecialHeaders.litellm_credential_header_names()) | {
    b"litellm_user_api_key",
    b"x-mcp-auth",
}
_PUBLIC_READ_PATHS: Final = frozenset(
    {
        "/",
        "/favicon.ico",
        "/robots.txt",
        "/health/liveness",
        "/health/liveliness",
        "/health/readiness",
        "/ui",
        "/ui/",
        "/ui/welcome",
        "/ui/welcome/",
        "/ui/login",
        "/ui/login/",
        "/ui/register",
        "/ui/register/",
        "/ui/models",
        "/ui/models/",
        "/ui/developers",
        "/ui/developers/",
        "/ui/pricing",
        "/ui/pricing/",
    }
)
_PUBLIC_STATIC_PREFIXES: Final = ("/ui/_next/", "/ui/assets/", "/static/", "/_next/")
_UNAVAILABLE_BODY: Final = b'{"error":{"message":"Gateway coordination is temporarily unavailable. Retry shortly.","type":"coordination_unavailable","code":"coordination_unavailable"}}'
_request_generation: Final[ContextVar[object | None]] = ContextVar(
    "retail_coordination_request_generation", default=None
)


@dataclass(frozen=True, slots=True)
class GuardConfig:
    required: bool = False
    interval: float = 0.5
    timeout: float = 0.5
    max_age: float = 2.0

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.interval, self.timeout, self.max_age)):
            raise ValueError("Coordination guard timing must be finite")
        if not 0.05 <= self.interval <= 0.5 or not 0.05 <= self.timeout <= 0.5:
            raise ValueError("Coordination guard interval and timeout must be between 0.05 and 0.5 seconds")
        if not self.interval + self.timeout <= self.max_age <= 2:
            raise ValueError("Coordination guard max age must cover a probe cycle and must not exceed two seconds")

    @classmethod
    def from_environment(cls) -> "GuardConfig":
        required: Final = os.environ.get("RETAIL_REQUIRE_COORDINATION_REDIS", "false").lower()
        if required not in {"true", "false"}:
            raise ValueError("RETAIL_REQUIRE_COORDINATION_REDIS must be true or false")
        return cls(
            required=required == "true",
            interval=float(os.environ.get("RETAIL_COORDINATION_HEALTH_INTERVAL_SECONDS", "0.5")),
            timeout=float(os.environ.get("RETAIL_COORDINATION_HEALTH_TIMEOUT_SECONDS", "0.5")),
            max_age=float(os.environ.get("RETAIL_COORDINATION_HEALTH_MAX_AGE_SECONDS", "2")),
        )


class CoordinationGuardStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    required: bool
    state: Literal["unknown", "healthy", "unhealthy", "stale"]
    accepting_credentials: bool
    reason: str
    checked_age_seconds: float | None
    interval_seconds: float
    timeout_seconds: float
    max_age_seconds: float


@dataclass(frozen=True, slots=True)
class _Snapshot:
    checked_at: float | None = None
    healthy: bool = False
    reason: str = "not_checked"


class _PingClient(Protocol):
    def ping(self) -> Awaitable[bool]: ...


class UnsupportedAuthCache(ValueError):
    pass


class UnsupportedReadReplica(UnsupportedAuthCache):
    pass


def validate_cache_configuration(cache: UserApiKeyCache, *, read_replica: bool = False) -> None:
    if read_replica:
        raise UnsupportedReadReplica("Strict coordination guard does not support DATABASE_URL_READ_REPLICA routing")
    if cache.redis_cache is not None or cache.key_object_cache.redis_cache is not None:
        raise UnsupportedAuthCache(
            "Strict coordination guard requires enable_redis_auth_cache=false; shared auth-object recovery is unsupported"
        )


def validate_native_auth_cache() -> None:
    from litellm.proxy import proxy_server

    prisma: Final = proxy_server.prisma_client
    replica: Final = bool(os.environ.get("DATABASE_URL_READ_REPLICA")) or bool(
        prisma is not None and isinstance(prisma.db, RoutingPrismaWrapper)
    )
    validate_cache_configuration(proxy_server.user_api_key_cache, read_replica=replica)


async def native_coordination_probe() -> bool:
    from litellm.proxy.common_utils.config_sync_pubsub import coordination_redis_cache

    validate_native_auth_cache()
    cache: Final = coordination_redis_cache()
    if cache is None:
        return False
    client: Final[_PingClient] = cache.init_async_client()
    return await client.ping()


def reset_local_auth_objects(cache: UserApiKeyCache) -> None:
    cache.in_memory_cache.flush_cache()
    cache.key_object_cache.in_memory_cache.flush_cache()


async def native_recovery_reset() -> None:
    from litellm.proxy import proxy_server

    validate_native_auth_cache()
    await asyncio.to_thread(reset_local_auth_objects, proxy_server.user_api_key_cache)


def native_credential_headers() -> frozenset[bytes]:
    from litellm.proxy import proxy_server

    configured: Final = proxy_server.general_settings.get("litellm_key_header_name")
    return _CREDENTIAL_HEADERS | ({configured.lower().encode("latin-1")} if isinstance(configured, str) else set())


class CoordinationMonitor:
    def __init__(
        self,
        config: GuardConfig,
        *,
        probe: Callable[[], Awaitable[bool]] = native_coordination_probe,
        recover: Callable[[], Awaitable[None]] = native_recovery_reset,
        clock: Callable[[], float] = time.monotonic,
        credential_headers: Callable[[], frozenset[bytes]] = native_credential_headers,
    ) -> None:
        self.config = config
        self._probe = probe
        self._recover = recover
        self._clock = clock
        self._credential_headers = credential_headers
        self.headers = credential_headers()
        self.generation = object()
        self._snapshot = _Snapshot()
        self._task: asyncio.Task[None] | None = None

    def healthy(self, now: float | None = None) -> bool:
        checked: Final = self._snapshot.checked_at
        return bool(
            self._snapshot.healthy
            and checked is not None
            and (self._clock() if now is None else now) - checked <= self.config.max_age
        )

    def accepting(self, now: float | None = None) -> bool:
        return not self.config.required or self.healthy(now)

    def cache_writes_allowed(self) -> bool:
        return _request_generation.get() is self.generation and self.healthy()

    def _invalidate(self, reason: str, checked_at: float | None = None) -> None:
        self.generation = object()
        self._snapshot = _Snapshot(checked_at, False, reason)

    def status(self, now: float | None = None) -> CoordinationGuardStatus:
        checked: Final = self._snapshot.checked_at
        age: Final = max(0, (self._clock() if now is None else now) - checked) if checked is not None else None
        state: Final = (
            "unknown"
            if checked is None
            else "stale"
            if age is not None and age > self.config.max_age
            else "healthy"
            if self._snapshot.healthy
            else "unhealthy"
        )
        return CoordinationGuardStatus(
            required=self.config.required,
            state=state,
            accepting_credentials=not self.config.required or state == "healthy",
            reason="health_snapshot_expired" if state == "stale" else self._snapshot.reason,
            checked_age_seconds=age,
            interval_seconds=self.config.interval,
            timeout_seconds=self.config.timeout,
            max_age_seconds=self.config.max_age,
        )

    async def check_once(self) -> None:
        try:
            self.headers = self._credential_headers()
            async with asyncio.timeout(self.config.timeout):
                available: Final = await self._probe()
            verified_at: Final = self._clock()
            if not available:
                self._invalidate("redis_unavailable", verified_at)
                return
            if self.status().state != "healthy":
                self._invalidate("recovering_auth_objects", self._clock())
                await self._recover()
            self._snapshot = _Snapshot(verified_at, True, "redis_verified")
        except UnsupportedReadReplica:
            self._invalidate("read_replica_unsupported", self._clock())
        except UnsupportedAuthCache:
            self._invalidate("shared_auth_cache_unsupported", self._clock())
        except TimeoutError:
            self._invalidate("redis_timeout", self._clock())
        except Exception:  # noqa: BLE001  # transport details and credentials must not escape health snapshots
            self._invalidate("redis_unavailable", self._clock())

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="retail-coordination-health")

    async def _run(self) -> None:
        while True:
            await self.check_once()
            await asyncio.sleep(self.config.interval)

    async def close(self) -> None:
        self._invalidate("monitor_stopped")
        task: Final = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.wait((task,), timeout=1)


def request_path(scope: Scope) -> str:
    path: Final = scope.get("path", "")
    root: Final = scope.get("root_path", "").rstrip("/")
    return path[len(root) :] if root and path.startswith(root + "/") else path


def public_request(scope: Scope) -> bool:
    method: Final = scope.get("method", "")
    route: Final = request_path(scope)
    return bool(
        (method in {"GET", "HEAD"} and (route in _PUBLIC_READ_PATHS or route.startswith(_PUBLIC_STATIC_PREFIXES)))
        or (route in {"/login", "/login/"} and method in {"GET", "POST"})
    )


def carries_credentials(headers: Sequence[tuple[bytes, bytes]], query: bytes, names: frozenset[bytes]) -> bool:
    if len(headers) > 128 or len(query) > 8192 or query.count(b"&") > 255:
        return True
    for name, value in headers:
        lowered: Final = name.lower()
        if lowered in names:
            return True
        if lowered == b"sec-websocket-protocol" and b"openai-insecure-api-key." in value:
            return True
        if lowered == b"cookie":
            if len(value) > 8192 or value.count(b";") > 63:
                return True
            for cookie in value.split(b";", 64):
                if cookie.partition(b"=")[0].strip().lower() == b"token":
                    return True
    for field in query.split(b"&", 256):
        key: Final = field.partition(b"=")[0]
        if len(key) <= 128 and unquote_to_bytes(key.replace(b"+", b" ")).lower() in {
            b"key",
            b"api_key",
            b"access_token",
        }:
            return True
    return False


class _GuardLifecycle:
    def __init__(self) -> None:
        self.monitor: CoordinationMonitor | None = None
        self.lock = asyncio.Lock()


_lifecycle: Final = _GuardLifecycle()


async def start_coordination_guard() -> None:
    from litellm.proxy import proxy_server

    async with _lifecycle.lock:
        if _lifecycle.monitor is not None:
            return
        config: Final = GuardConfig.from_environment()
        if not config.required:
            proxy_server.user_api_key_cache.set_write_guard(None)
            return
        validate_native_auth_cache()
        monitor: Final = CoordinationMonitor(config)
        proxy_server.user_api_key_cache.set_write_guard(monitor.cache_writes_allowed)
        _lifecycle.monitor = monitor
        await monitor.start()


async def close_coordination_guard() -> None:
    async with _lifecycle.lock:
        monitor: Final = _lifecycle.monitor
        _lifecycle.monitor = None
    if monitor is not None:
        await monitor.close()


def coordination_guard_status() -> CoordinationGuardStatus:
    monitor: Final = _lifecycle.monitor
    return (monitor or CoordinationMonitor(GuardConfig.from_environment())).status()


class CoordinationGuardMiddleware:
    def __init__(
        self, app: ASGIApp, *, monitor: CoordinationMonitor | None = None, config: GuardConfig | None = None
    ) -> None:
        self.app = app
        self.monitor = monitor
        self.config = config if config is not None else GuardConfig.from_environment()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.config.required or scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        monitor: Final = self.monitor if self.monitor is not None else _lifecycle.monitor
        token: Final = _request_generation.set(monitor.generation if monitor is not None else None)
        try:
            await self._dispatch(scope, receive, send, monitor)
        finally:
            _request_generation.reset(token)

    async def _dispatch(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        monitor: CoordinationMonitor | None,
    ) -> None:
        readiness: Final = (
            scope["type"] == "http"
            and scope.get("method") in {"GET", "HEAD"}
            and request_path(scope) == "/health/readiness"
        )
        if not readiness and public_request(scope):
            await self.app(scope, receive, send)
            return
        names: Final = monitor.headers if monitor is not None else _CREDENTIAL_HEADERS
        if not readiness and not carries_credentials(scope.get("headers", ()), scope.get("query_string", b""), names):
            await self.app(scope, receive, send)
            return
        if monitor is not None and monitor.healthy():
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013, "reason": "Gateway coordination unavailable"})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_UNAVAILABLE_BODY)).encode()),
                    (b"cache-control", b"private, no-store"),
                    (b"retry-after", b"1"),
                    (b"vary", b"Authorization, Cookie"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else _UNAVAILABLE_BODY})
