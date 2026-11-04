import asyncio
import os
from collections.abc import Mapping
from typing import Final
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException
from pydantic import JsonValue, SecretStr
from redis.asyncio import Redis

from litellm._logging import verbose_proxy_logger

from .control_plane_flags import oidc_sso_enabled
from .coordination_guard import coordination_guard_status
from .native_audit_store import ensure_native_schema
from .oidc_config import OidcConfig, OidcFailure, load_oidc_config
from .oidc_runtime import OidcRuntime, OidcStatus
from .oidc_sessions import BindingSpec, MintedOIDCSession, NativeOIDCSessionStore, SessionSettings


def session_general_settings(settings: Mapping[str, object]) -> Mapping[str, JsonValue] | OidcFailure:
    header: Final = settings.get("litellm_key_header_name")
    if header is None:
        return {}
    if not isinstance(header, str) or not 1 <= len(header) <= 128 or any(
        ord(character) < 33 or ord(character) > 126 for character in header
    ):
        return OidcFailure("invalid")
    return {"litellm_key_header_name": header}


def _server_master_key() -> str | None:
    from litellm.proxy import proxy_server

    value: Final = proxy_server.master_key
    return value if isinstance(value, str) and value.strip() else None


def session_settings(config: OidcConfig) -> SessionSettings:
    return SessionSettings(
        namespace=config.namespace, generation=config.generation, digest=config.digest(), issuer=config.issuer,
        session_seconds=config.session_seconds,
        bindings=tuple(
            BindingSpec(subject=row.subject, user_id=row.user_id, enabled=row.enabled) for row in config.bindings
        ),
    )


def dedicated_redis() -> Redis:
    url: Final = os.environ.get("REDIS_URL")
    if url:
        allowed: Final = frozenset({
            "db", "ssl_cert_reqs", "ssl_check_hostname", "ssl_ca_certs", "ssl_certfile", "ssl_keyfile",
        })
        try:
            fields: Final = tuple(parse_qsl(urlsplit(url).query, strict_parsing=True, max_num_fields=8))
            if len(fields) != len(frozenset(name for name, _ in fields)) or any(name not in allowed for name, _ in fields):
                raise ValueError("Invalid sign-in coordination configuration")
        except ValueError:
            raise ValueError("Invalid sign-in coordination configuration") from None
        return Redis.from_url(
            url, max_connections=4, socket_connect_timeout=1, socket_timeout=1,
            decode_responses=False, health_check_interval=30,
        )
    return Redis(
        host=os.environ["REDIS_HOST"], port=int(os.environ.get("REDIS_PORT", "6379")),
        username=os.environ.get("REDIS_USERNAME"), password=os.environ.get("REDIS_PASSWORD"),
        db=int(os.environ.get("REDIS_DB", "0")), ssl=os.environ.get("REDIS_SSL", "false").lower() == "true",
        max_connections=4, socket_connect_timeout=1, socket_timeout=1,
        decode_responses=False, health_check_interval=30,
    )


class OidcService:
    def __init__(self, config: OidcConfig, runtime: OidcRuntime, store: NativeOIDCSessionStore, redis: Redis) -> None:
        self.config = config
        self.runtime = runtime
        self.store = store
        self.redis = redis
        self.ready = False

    async def warm(self) -> None:
        await self.store.initialize()
        await self.store.configure(session_settings(self.config))
        await self.runtime.start()
        self.ready = True

    def status(self) -> OidcStatus:
        return OidcStatus(
            enabled=True, label=self.config.label,
            available=(
                self.ready and _server_master_key() is not None
                and coordination_guard_status().accepting_credentials and self.runtime.status().available
            ),
        )

    async def mint(self, subject: str) -> MintedOIDCSession:
        from litellm.proxy import proxy_server

        general: Final = session_general_settings(proxy_server.general_settings)
        if isinstance(general, OidcFailure):
            raise HTTPException(503, "Sign-in is temporarily unavailable")
        return await self.store.mint(
            session_settings(self.config), subject,
            general_settings=general,
            premium_user=bool(proxy_server.premium_user), master_key=_server_master_key() or "",
        )

    async def close(self) -> None:
        self.ready = False
        await self.runtime.close()
        await self.store.close()
        await self.redis.aclose(close_connection_pool=True)


class _Lifecycle:
    def __init__(self) -> None:
        self.service: OidcService | None = None
        self.task: asyncio.Task[None] | None = None
        self.label = "Organization sign in"


_LIFECYCLE: Final = _Lifecycle()


async def _warm(config: OidcConfig) -> None:
    from litellm.proxy import proxy_server

    while oidc_sso_enabled():
        try:
            native: Final = proxy_server.prisma_client
            if native is None:
                await asyncio.sleep(5)
                continue
            if _LIFECYCLE.service is None:
                secret: Final = os.environ.get(config.client_secret_env, "")
                if not secret or len(secret) > 8192 or any(
                    ord(value) < 32 or ord(value) == 127 for value in secret
                ):
                    await asyncio.sleep(5)
                    continue
                redis: Final = dedicated_redis()
                _LIFECYCLE.service = OidcService(
                    config, OidcRuntime(config, redis, SecretStr(secret)), NativeOIDCSessionStore(native), redis,
                )
            await ensure_native_schema(native)
            await _LIFECYCLE.service.warm()
        except Exception as exc:  # noqa: BLE001  # optional sign-in failure must not block normal authentication or inference
            verbose_proxy_logger.warning("Retail organization sign-in startup deferred (%s)", type(exc).__name__)
        else:
            return
        await asyncio.sleep(5)


async def start_oidc_service() -> None:
    if not oidc_sso_enabled() or _LIFECYCLE.task is not None:
        return
    config: Final = load_oidc_config()
    if not isinstance(config, OidcConfig):
        return
    _LIFECYCLE.label = config.label
    _LIFECYCLE.task = asyncio.create_task(_warm(config), name="retail-oidc-startup")


async def close_oidc_service() -> None:
    task: Final = _LIFECYCLE.task
    service: Final = _LIFECYCLE.service
    _LIFECYCLE.task = None
    _LIFECYCLE.service = None
    _LIFECYCLE.label = "Organization sign in"
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if service is not None:
        await service.close()


def oidc_status() -> OidcStatus:
    enabled: Final = oidc_sso_enabled()
    service: Final = _LIFECYCLE.service
    if enabled and service is not None:
        return service.status()
    return OidcStatus(enabled=enabled, available=False, label=_LIFECYCLE.label)


def get_oidc_service() -> OidcService | OidcFailure:
    service: Final = _LIFECYCLE.service
    if not oidc_sso_enabled() or service is None or not service.status().available:
        return OidcFailure("unavailable")
    return service
