import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from litellm.proxy._types import LiteLLM_UserTable, ProxyErrorTypes, ProxyException, UserAPIKeyAuth
from litellm.proxy.utils import hash_token_if_needed

from .api_jwt_auth import api_jwt_compatible
from .coordination_guard import coordination_guard_status
from .native_audit_store import NativeAuditWriter

SCIM_USER_PREFIX: Final = "retail-scim-user:"
_SCOPE: Final = "retail_scim_identity"
_SLOTS: Final = asyncio.Semaphore(4)
_FAILED: Final = "Account credential authentication failed"
_UNAVAILABLE: Final = "Account authority is temporarily unavailable"
_GROUP_ACCESS: Final = ''' AND EXISTS (
    SELECT 1 FROM retail_scim_settings s WHERE s.singleton AND s.namespace=r.namespace
    AND (cardinality(s.access_group_external_ids)=0 OR EXISTS (
        SELECT 1 FROM retail_scim_groups g JOIN retail_scim_group_members m ON m.group_id=g.id
        WHERE m.user_resource_id=r.id AND NOT m.removed AND g.namespace=r.namespace AND NOT g.deleted
        AND g.external_id=ANY(s.access_group_external_ids))))'''


@dataclass(frozen=True, slots=True)
class ResolvedScimIdentity:
    auth: UserAPIKeyAuth = field(repr=False)
    user: LiteLLM_UserTable = field(repr=False)
    target_hash: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Attempt:
    pass


class _Identity(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    key_data: Mapping[str, JsonValue] = Field(repr=False)
    user_data: Mapping[str, JsonValue] = Field(repr=False)


class _Directory(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    user_id: str


_IDENTITIES: Final = TypeAdapter(tuple[_Identity, ...])
_DIRECTORY: Final = TypeAdapter(tuple[_Directory, ...])


def is_scim_user(user_id: object) -> bool:
    return isinstance(user_id, str) and user_id.startswith(SCIM_USER_PREFIX)


def _context(request: Request) -> ResolvedScimIdentity | _Attempt | None:
    scope: Final = getattr(request, "scope", None)
    value: Final = scope.get(_SCOPE) if isinstance(scope, dict) else None
    return value if isinstance(value, (ResolvedScimIdentity, _Attempt)) else None


def scim_identity_request(request: Request) -> bool:
    return _context(request) is not None


def scim_identity_user(request: Request) -> LiteLLM_UserTable | None:
    context: Final = _context(request)
    return context.user if isinstance(context, ResolvedScimIdentity) else None


def effective_scim_key(request: Request, fallback: str | None) -> str | None:
    context: Final = _context(request)
    return context.target_hash if isinstance(context, ResolvedScimIdentity) else fallback


def _require_environment() -> None:
    health: Final = coordination_guard_status()
    if (
        os.environ.get("RETAIL_BILLING_ENABLED", "false").lower() != "true"
        or not health.required or not health.accepting_credentials or not api_jwt_compatible()
    ):
        raise HTTPException(503, _UNAVAILABLE)


async def _read_identity(reader: NativeAuditWriter, target_hash: str, user_id: str) -> ResolvedScimIdentity:
    rows: Final = _IDENTITIES.validate_python(await reader.query_raw(
        '''SELECT to_jsonb(k) AS key_data,to_jsonb(u)-'password'-'last_breach_check_at' AS user_data
        FROM "LiteLLM_VerificationToken" k JOIN "LiteLLM_UserTable" u ON u.user_id=k.user_id
        JOIN retail_scim_users r ON r.user_id=u.user_id AND r.active AND NOT r.deleted
        JOIN retail_scim_credentials p ON p.token_hash=k.token AND p.user_id=u.user_id
        AND p.credential_revision=r.credential_revision
        WHERE k.token=$1 AND k.user_id=$2 AND u.user_role='customer'
        AND u.password_reset_required IS DISTINCT FROM true AND k.blocked IS DISTINCT FROM true
        AND (k.expires IS NULL OR (k.expires AT TIME ZONE 'UTC')>clock_timestamp())''' + _GROUP_ACCESS,
        target_hash, user_id,
    ))
    if len(rows) != 1:
        raise HTTPException(401, _FAILED)
    user: Final = LiteLLM_UserTable.model_validate(rows[0].user_data)
    auth: Final = UserAPIKeyAuth.model_validate(rows[0].key_data).model_copy(update={
        "api_key": target_hash, "token": target_hash, "user_id": user.user_id,
        "user_role": "customer", "jwt_claims": None, "via_virtual_key": True,
    })
    if (
        rows[0].key_data.get("token") != target_hash or rows[0].key_data.get("user_id") != user_id
        or user.user_id != user_id or user.user_role != "customer" or user.password_reset_required is True
    ):
        raise HTTPException(401, _FAILED)
    return ResolvedScimIdentity(auth, user, target_hash)


async def _native_identity(target_hash: str, user_id: str) -> ResolvedScimIdentity:
    from litellm.proxy import proxy_server

    client: Final = proxy_server.prisma_client
    if client is None:
        raise HTTPException(503, _UNAVAILABLE)
    async with client.tx(timeout=timedelta(seconds=2)) as writer:
        await writer.execute_raw("SET LOCAL lock_timeout='500ms'")
        await writer.execute_raw("SET LOCAL statement_timeout='1500ms'")
        return await _read_identity(writer, target_hash, user_id)


async def resolve_scim_identity(
    request: Request, auth: UserAPIKeyAuth | None, presented_key: str | None,
    *, reader: NativeAuditWriter | None = None,
) -> ResolvedScimIdentity | None:
    user_id: Final = auth.user_id if auth is not None else None
    if auth is None or not isinstance(user_id, str) or not is_scim_user(user_id):
        return None
    request.scope[_SCOPE] = _Attempt()  # rebind-ok: identify private managed failures before any native fallback
    _require_environment()
    if not isinstance(presented_key, str) or not presented_key or auth.is_session_token:
        raise HTTPException(401, _FAILED)
    target_hash: Final = hash_token_if_needed(presented_key)
    try:
        async with asyncio.timeout(2), _SLOTS:
            resolved: Final = (
                await _read_identity(reader, target_hash, user_id)
                if reader is not None else await _native_identity(target_hash, user_id)
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001  # every unavailable authority failure must remain private and fail closed
        raise HTTPException(503, _UNAVAILABLE) from None
    request.scope[_SCOPE] = resolved  # rebind-ok: retain only native hash and current authority, never a raw bearer
    return resolved


def require_scim_identity_result(request: Request, auth: UserAPIKeyAuth) -> None:
    context: Final = _context(request)
    if context is None and not is_scim_user(auth.user_id):
        return
    if not isinstance(context, ResolvedScimIdentity):
        raise HTTPException(401, _FAILED)
    _require_environment()
    if (
        auth.user_id != context.user.user_id or auth.user_role != "customer"
        or auth.api_key != context.target_hash or auth.token != context.target_hash
        or auth.jwt_claims is not None or auth.is_session_token or auth.authenticated_by_custom_auth
    ):
        raise HTTPException(401, _FAILED)


def reject_scim_password_login(user_id: object) -> None:
    if is_scim_user(user_id):
        raise HTTPException(403, "Password sign-in is unavailable for this account")


async def require_scim_directory_user(reader: NativeAuditWriter, user_id: str, *, lock: bool = False) -> None:
    if not is_scim_user(user_id):
        return
    _require_environment()
    rows: Final = _DIRECTORY.validate_python(await reader.query_raw(
        '''SELECT r.user_id FROM retail_scim_users r JOIN "LiteLLM_UserTable" u ON u.user_id=r.user_id
        WHERE r.user_id=$1 AND r.active AND NOT r.deleted AND u.user_role='customer'
        AND u.password_reset_required IS DISTINCT FROM true''' + _GROUP_ACCESS
        + (" FOR SHARE OF r" if lock else ""), user_id,
    ))
    if len(rows) != 1 or rows[0].user_id != user_id:
        raise HTTPException(401, _FAILED)


async def require_scim_session(reader: NativeAuditWriter, user_id: str, target_hash: str) -> None:
    if is_scim_user(user_id):
        _require_environment()
        await _read_identity(reader, target_hash, user_id)


def private_scim_identity_exception(error: Exception, request: Request) -> Exception:
    if not scim_identity_request(request):
        return error
    code: Final = error.code if isinstance(error, ProxyException) else getattr(error, "status_code", None)
    status: Final = int(str(code)) if str(code) in {"401", "402", "403", "429", "503"} else 401
    return ProxyException(
        message=_UNAVAILABLE if status == 503 else _FAILED, type=ProxyErrorTypes.auth_error, param=None, code=status,
    )
