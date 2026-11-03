from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Protocol

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, TypeAdapter

from litellm.constants import LITELLM_PROXY_MASTER_KEY_ALIAS
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

from .store import WalletStore


@dataclass(frozen=True, slots=True)
class ControlPlaneActor:
    user_id: str
    native_role: str
    is_admin: bool
    can_write: bool


class NativeUserState(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    user_role: str | None = None
    password_reset_required: bool | None = None


class NativeCredentialState(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str | None = None
    team_id: str | None = None
    blocked: bool | None = None
    expires: datetime | None = None
    allowed_routes: tuple[str, ...] = ()


class NativeIdentityReader(Protocol):
    async def user(self, user_id: str) -> NativeUserState | None: ...

    async def users(self, user_ids: tuple[str, ...]) -> tuple[NativeUserState, ...]: ...

    async def credential(self, token: str) -> NativeCredentialState | None: ...


class _NativeWriter(Protocol):
    async def query_raw(self, query: str, *args: object) -> object: ...


class _NativeClient(Protocol):
    writer_db: _NativeWriter


_USER_ROWS: Final = TypeAdapter(tuple[NativeUserState, ...])
_CREDENTIAL_ROWS: Final = TypeAdapter(tuple[NativeCredentialState, ...])
_READ_ONLY_ROLES: Final = frozenset(
    (LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY.value, LitellmUserRoles.INTERNAL_USER_VIEW_ONLY.value)
)
_ALLOWED_ROLES: Final = frozenset(role.value for role in LitellmUserRoles)


def native_master_actor(auth: UserAPIKeyAuth, configured_master_present: bool) -> bool:
    return bool(
        configured_master_present
        and auth.api_key == LITELLM_PROXY_MASTER_KEY_ALIAS
        and auth.via_virtual_key is True
        and auth.user_role == LitellmUserRoles.PROXY_ADMIN
        and auth.authenticated_by_custom_auth is False
    )


def _configured_master_present() -> bool:
    from litellm.proxy import proxy_server

    return bool(proxy_server.master_key)


class PrismaIdentityReader:
    def __init__(self, writer: _NativeWriter) -> None:
        self._writer = writer

    async def user(self, user_id: str) -> NativeUserState | None:
        rows: Final = _USER_ROWS.validate_python(
            await self._writer.query_raw(
                'SELECT user_id,user_role,password_reset_required FROM "LiteLLM_UserTable" WHERE user_id=$1', user_id
            )
        )
        return rows[0] if rows else None

    async def credential(self, token: str) -> NativeCredentialState | None:
        rows: Final = _CREDENTIAL_ROWS.validate_python(
            await self._writer.query_raw(
                'SELECT user_id,team_id,blocked,expires,allowed_routes FROM "LiteLLM_VerificationToken" WHERE token=$1',
                token,
            )
        )
        return rows[0] if rows else None

    async def users(self, user_ids: tuple[str, ...]) -> tuple[NativeUserState, ...]:
        return _USER_ROWS.validate_python(
            await self._writer.query_raw(
                """SELECT user_id,user_role,password_reset_required FROM "LiteLLM_UserTable"
                WHERE user_id=ANY($1::text[])""",
                list(user_ids),
            )
        )


def native_identity_reader() -> NativeIdentityReader:
    from litellm.proxy import proxy_server

    client: Final[_NativeClient | None] = proxy_server.prisma_client
    if client is None:
        raise HTTPException(503, "Account authority is temporarily unavailable")
    return PrismaIdentityReader(client.writer_db)


async def _read_user(reader: NativeIdentityReader, user_id: str) -> NativeUserState | None:
    try:
        return await reader.user(user_id)
    except Exception as exc:  # noqa: BLE001  # authority failures must never reuse a cached permission grant
        raise HTTPException(503, "Account authority is temporarily unavailable") from exc


def _active_user(user: NativeUserState | None) -> bool:
    return bool(user and user.user_role in _ALLOWED_ROLES and user.password_reset_required is not True)


async def verify_target_user(user_id: str, *, reader: NativeIdentityReader | None = None) -> None:
    source: Final = reader if reader is not None else native_identity_reader()
    if not _active_user(await _read_user(source, user_id)):
        raise HTTPException(404, "The requested account is unavailable")


async def active_target_users(
    user_ids: tuple[str, ...], *, reader: NativeIdentityReader | None = None
) -> frozenset[str]:
    if len(user_ids) > 1000:
        raise HTTPException(422, "Account checks exceed the bounded management page limit")
    if not user_ids:
        return frozenset()
    source: Final = reader if reader is not None else native_identity_reader()
    try:
        users: Final = await source.users(user_ids)
    except Exception as exc:  # noqa: BLE001  # missing authority must never grant access to a report target
        raise HTTPException(503, "Account authority is temporarily unavailable") from exc
    requested: Final = frozenset(user_ids)
    return frozenset(user.user_id for user in users if user.user_id in requested and _active_user(user))


async def verify_actor(
    auth: UserAPIKeyAuth,
    store: WalletStore | None = None,
    *,
    reader: NativeIdentityReader | None = None,
    now: datetime | None = None,
    configured_master_present: bool | None = None,
) -> ControlPlaneActor:
    if not auth.user_id or not auth.api_key:
        raise HTTPException(403, "A database-backed account credential is required")
    if auth.api_key == LITELLM_PROXY_MASTER_KEY_ALIAS:
        configured: Final = (
            _configured_master_present() if configured_master_present is None else configured_master_present
        )
        if not native_master_actor(auth, configured):
            raise HTTPException(403, "A verified native master credential is required")
        return ControlPlaneActor(auth.user_id, LitellmUserRoles.PROXY_ADMIN.value, True, True)
    source: Final = reader if reader is not None else native_identity_reader()
    current: Final = await _read_user(source, auth.user_id)
    if not _active_user(current) or current is None:
        raise HTTPException(403, "This account cannot access the control plane")
    try:
        credential: Final = await source.credential(auth.api_key)
    except Exception as exc:  # noqa: BLE001  # unavailable credential authority must not reuse cached authorization
        raise HTTPException(503, "Account authority is temporarily unavailable") from exc
    checked_at: Final = now if now is not None else datetime.now(timezone.utc)
    if credential is None or credential.user_id != current.user_id or credential.blocked is True:
        raise HTTPException(403, "This account credential is no longer active")
    expiry: Final = (
        credential.expires.replace(tzinfo=timezone.utc)
        if credential.expires is not None and credential.expires.tzinfo is None
        else credential.expires
    )
    if expiry is not None and expiry <= checked_at:
        raise HTTPException(403, "This account credential is no longer active")
    role: Final = current.user_role or ""
    if auth.request_route:
        from litellm.proxy.auth.route_checks import RouteChecks

        fresh: Final = auth.model_copy(
            update={
                "allowed_routes": list(credential.allowed_routes),
                "user_role": LitellmUserRoles(role),
                "team_id": credential.team_id,
            }
        )
        RouteChecks.is_virtual_key_allowed_to_call_route(route=auth.request_route, valid_token=fresh)
    return ControlPlaneActor(
        user_id=current.user_id,
        native_role=role,
        is_admin=role == LitellmUserRoles.PROXY_ADMIN.value and auth.user_role == LitellmUserRoles.PROXY_ADMIN,
        can_write=role not in _READ_ONLY_ROLES,
    )
