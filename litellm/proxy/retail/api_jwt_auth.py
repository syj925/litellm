import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from fastapi import HTTPException, Request
from pydantic import SecretStr

from litellm.proxy._types import LiteLLM_UserTable, ProxyErrorTypes, ProxyException, UserAPIKeyAuth
from litellm.proxy.db.exception_handler import PrismaDBExceptionHandler
from litellm.proxy.db.routing_prisma_wrapper import RoutingPrismaWrapper

from .api_jwt_config import ApiJwtFailure
from .api_jwt_store import ResolvedApiJwt
from .coordination_guard import coordination_guard_status, request_path

API_JWT_PREFIX: Final = "rt-jwt."
API_JWT_SCOPE_KEY: Final = "retail_api_jwt_context"
API_JWT_ROUTES: Final = frozenset({
    "/chat/completions", "/v1/chat/completions", "/messages", "/v1/messages", "/responses", "/v1/responses",
})


@dataclass(frozen=True, slots=True)
class ApiJwtInput:
    token: SecretStr = field(repr=False)


@dataclass(frozen=True, slots=True)
class ApiJwtAttempt:
    pass


@dataclass(frozen=True, slots=True)
class ApiJwtContext:
    user: LiteLLM_UserTable = field(repr=False)
    target_hash: str = field(repr=False)


def _context(request: Request) -> ApiJwtInput | ApiJwtAttempt | ApiJwtContext | None:
    scope: Final = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return None
    value: Final = scope.get(API_JWT_SCOPE_KEY)
    return value if isinstance(value, (ApiJwtInput, ApiJwtAttempt, ApiJwtContext)) else None


def api_jwt_request(request: Request) -> bool:
    return _context(request) is not None


def api_jwt_raw_key(value: str | None) -> bool:
    return isinstance(value, str) and API_JWT_PREFIX in value.lower()


def api_jwt_user(request: Request) -> LiteLLM_UserTable | None:
    context: Final = _context(request)
    return context.user if isinstance(context, ApiJwtContext) else None


def effective_api_jwt_key(request: Request, fallback: str | None) -> str | None:
    context: Final = _context(request)
    return context.target_hash if isinstance(context, ApiJwtContext) else fallback


def api_jwt_settings_compatible(settings: Mapping[str, object]) -> bool:
    return not any(settings.get(name) for name in (
        "enable_jwt_auth", "enable_oauth2_auth", "enable_oauth2_proxy_auth", "litellm_key_header_name",
    ))


def api_jwt_compatible() -> bool:
    import litellm
    from litellm.proxy import proxy_server

    from .billing import RetailBilling

    native: Final = proxy_server.prisma_client
    return (
        isinstance(proxy_server.master_key, str) and bool(proxy_server.master_key.strip())
        and native is not None and not isinstance(native.db, RoutingPrismaWrapper)
        and not os.environ.get("DATABASE_URL_READ_REPLICA") and proxy_server.user_custom_auth is None
        and api_jwt_settings_compatible(proxy_server.general_settings)
        and any(isinstance(callback, RetailBilling) for callback in litellm.callbacks)
    )


async def _resolve_input(credential: ApiJwtInput) -> ResolvedApiJwt | ApiJwtFailure:
    from litellm.proxy import proxy_server
    from litellm.proxy.utils import hash_token

    from .api_jwt_service import get_api_jwt_service

    if not api_jwt_compatible():
        return ApiJwtFailure("unavailable")
    service: Final = get_api_jwt_service()
    if isinstance(service, ApiJwtFailure):
        return service
    verified: Final = await service.runtime.verify(credential.token)
    if isinstance(verified, ApiJwtFailure):
        return verified
    resolved: Final = await service.store.fresh(service.config, verified)
    if isinstance(resolved, ApiJwtFailure):
        return resolved
    if (
        resolved.user.user_id == proxy_server.litellm_proxy_admin_name
        or resolved.target_hash in {proxy_server.master_key, hash_token(proxy_server.master_key)}
    ):
        return ApiJwtFailure("invalid")
    return resolved


async def resolve_api_jwt_request(
    request: Request, api_key: str | None, *,
    credential_sources: tuple[str | None, ...] = (),
    resolve: Callable[[ApiJwtInput], Awaitable[ResolvedApiJwt | ApiJwtFailure]] = _resolve_input,
) -> ResolvedApiJwt | None:
    credential: Final = _context(request)
    if credential is None and not api_jwt_raw_key(api_key) and not any(api_jwt_raw_key(key) for key in credential_sources):
        return None
    request.scope[API_JWT_SCOPE_KEY] = ApiJwtAttempt()  # rebind-ok: consume the private credential before authentication logs and streaming
    if not isinstance(credential, ApiJwtInput) or request.method != "POST" or request_path(request.scope) not in API_JWT_ROUTES:
        raise HTTPException(401, "API access token authentication failed")
    try:
        result: Final = await resolve(credential)
    except Exception:  # noqa: BLE001  # transport and identity failures must not escape into native auth logs or spans
        raise HTTPException(503, "API access token authentication is unavailable") from None
    if isinstance(result, ApiJwtFailure):
        raise HTTPException(
            503 if result.code == "unavailable" else 401,
            "API access token authentication is unavailable" if result.code == "unavailable"
            else "API access token authentication failed",
        )
    request.scope[API_JWT_SCOPE_KEY] = ApiJwtContext(result.user, result.target_hash)  # rebind-ok: retain only verified native authority, never the JWT or claims
    return result


def require_api_jwt_result(request: Request, auth: UserAPIKeyAuth) -> None:
    context: Final = _context(request)
    if context is None:
        return
    if not isinstance(context, ApiJwtContext):
        raise HTTPException(401, "API access token authentication failed")
    if not coordination_guard_status().accepting_credentials or not api_jwt_compatible():
        raise HTTPException(503, "API access token authentication is unavailable")
    if (
        auth.user_id != context.user.user_id or auth.user_role != context.user.user_role
        or auth.user_role not in {"customer", "internal_user"}
        or auth.token != context.target_hash or auth.api_key != context.target_hash or auth.jwt_claims is not None
        or auth.team_id is not None or auth.org_id is not None or auth.project_id is not None
        or auth.agent_id is not None or auth.managed_agent_policy is not None or auth.is_session_token
    ):
        raise HTTPException(401, "API access token authentication failed")


def private_api_jwt_exception(error: Exception, request: Request, api_key: str | None = None) -> Exception:
    if not api_jwt_request(request) and not api_jwt_raw_key(api_key):
        return error
    status_code: Final = getattr(error, "status_code", None)
    native_code: Final = error.code if isinstance(error, ProxyException) else None
    code: Final = status_code if isinstance(status_code, (str, int)) else native_code
    safe_code: Final = (
        503 if isinstance(error, TimeoutError) or PrismaDBExceptionHandler.is_database_service_unavailable_error(error)
        else int(str(code)) if str(code) in {"401", "402", "403", "429", "503"} else 401
    )
    return ProxyException(
        message="API access token authentication is unavailable" if safe_code == 503
        else "API access token authentication failed", type=ProxyErrorTypes.auth_error, param=None, code=safe_code,
    )
