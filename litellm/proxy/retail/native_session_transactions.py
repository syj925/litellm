import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, TypeAlias

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, JsonValue, SecretStr, TypeAdapter

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import LiteLLM_VerificationToken, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.login_utils import PASSWORD_SESSION_METADATA
from litellm.proxy.auth.route_checks import RouteChecks

from .native_audit_store import NativeAuditTransaction, append_native_audit


class _User(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: str
    user_role: str | None
    password: SecretStr | None = None


class _Session(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: str | None
    team_id: str | None
    active: bool
    metadata: Mapping[str, JsonValue]
    allowed_routes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NativeSessionFailure:
    reason: Literal["inactive", "password_changed"]


@dataclass(frozen=True, slots=True)
class NativeSessionSuccess:
    changed: bool
    revoked: tuple[LiteLLM_VerificationToken, ...] = field(repr=False)

    def revoked_tokens(self) -> tuple[str, ...]:
        return tuple(row.token for row in self.revoked if row.token is not None)


NativeSessionOutcome: TypeAlias = NativeSessionFailure | NativeSessionSuccess
_USERS: Final = TypeAdapter(tuple[_User, ...])
_SESSIONS: Final = TypeAdapter(tuple[_Session, ...])
_TOKENS: Final = TypeAdapter(tuple[LiteLLM_VerificationToken, ...])
_ROLES: Final = frozenset(role.value for role in LitellmUserRoles)


def require_native_session_success(outcome: NativeSessionOutcome) -> NativeSessionSuccess:
    if isinstance(outcome, NativeSessionSuccess):
        return outcome
    match outcome.reason:
        case "inactive":
            raise HTTPException(403, "This dashboard session is no longer active")
        case "password_changed":
            raise HTTPException(409, "The password changed during verification. Retry with the current password.")


async def _lock_user(writer: NativeAuditTransaction, user_id: str) -> _User | None:
    rows: Final = _USERS.validate_python(
        await writer.query_raw(
            'SELECT user_id,user_role,password FROM "LiteLLM_UserTable" WHERE user_id=$1 FOR UPDATE',
            user_id,
        )
    )
    return rows[0] if rows else None


async def _lock_session(writer: NativeAuditTransaction, token: str) -> _Session | None:
    await writer.query_raw('SELECT token FROM "LiteLLM_VerificationToken" WHERE token=$1 FOR UPDATE', token)
    rows: Final = _SESSIONS.validate_python(
        await writer.query_raw(
            """SELECT user_id,team_id,metadata,allowed_routes,
            (blocked IS DISTINCT FROM true AND (expires IS NULL OR expires>clock_timestamp())) AS active
            FROM "LiteLLM_VerificationToken" WHERE token=$1""",
            token,
        )
    )
    return rows[0] if rows else None


def _valid_session(
    user: _User | None,
    session: _Session | None,
    auth: UserAPIKeyAuth,
    *,
    password_login: bool,
) -> bool:
    if user is None or user.user_role not in _ROLES or session is None:
        return False
    if session.user_id != user.user_id or session.team_id != UI_SESSION_TOKEN_TEAM_ID:
        return False
    if password_login and (
        not session.active
        or not all(session.metadata.get(key) == value for key, value in PASSWORD_SESSION_METADATA.items())
    ):
        return False
    if session.allowed_routes:
        fresh: Final = auth.model_copy(
            update={
                "allowed_routes": list(session.allowed_routes),
                "team_id": session.team_id,
                "user_role": LitellmUserRoles(user.user_role),
            }
        )
        RouteChecks.is_virtual_key_allowed_to_call_route(
            route="/user/password/change" if password_login else "/session/logout",
            valid_token=fresh,
        )
    return True


async def _delete_sessions(
    writer: NativeAuditTransaction,
    user_id: str,
    token: str,
    *,
    others: bool,
) -> tuple[LiteLLM_VerificationToken, ...]:
    return _TOKENS.validate_python(
        await writer.query_raw(
            """DELETE FROM "LiteLLM_VerificationToken" WHERE user_id=$1 AND team_id=$2
            AND (($4::boolean AND token<>$3) OR (NOT $4::boolean AND token=$3))
            RETURNING *,organization_id AS org_id""",
            user_id,
            UI_SESSION_TOKEN_TEAM_ID,
            token,
            others,
        )
    )


async def change_password_in_transaction(
    writer: NativeAuditTransaction,
    auth: UserAPIKeyAuth,
    *,
    expected_hash: str,
    replacement_hash: str,
) -> NativeSessionOutcome:
    if not auth.user_id or not auth.token:
        return NativeSessionFailure("inactive")
    user: Final = await _lock_user(writer, auth.user_id)
    session: Final = await _lock_session(writer, auth.token)
    if not _valid_session(user, session, auth, password_login=True) or user is None:
        return NativeSessionFailure("inactive")
    if user.password is None or not hmac.compare_digest(user.password.get_secret_value(), expected_hash):
        return NativeSessionFailure("password_changed")
    await writer.execute_raw(
        """UPDATE "LiteLLM_UserTable" SET password=$2,password_reset_required=false,
            last_breach_check_at=NULL,updated_at=clock_timestamp() WHERE user_id=$1""",
        user.user_id,
        replacement_hash,
    )
    revoked: Final = await _delete_sessions(writer, user.user_id, auth.token, others=True)
    await append_native_audit(writer, actor_id=user.user_id, action="native_password.changed")
    if revoked:
        await append_native_audit(
            writer,
            actor_id=user.user_id,
            action="native_session.revoked",
            affected_count=len(revoked),
        )
    return NativeSessionSuccess(True, revoked)


async def logout_in_transaction(writer: NativeAuditTransaction, auth: UserAPIKeyAuth) -> NativeSessionOutcome:
    if not auth.user_id:
        return NativeSessionFailure("inactive")
    if not auth.token:
        return NativeSessionSuccess(False, ())
    user: Final = await _lock_user(writer, auth.user_id)
    session: Final = await _lock_session(writer, auth.token)
    if session is None:
        return NativeSessionSuccess(False, ())
    if not _valid_session(user, session, auth, password_login=False):
        return NativeSessionFailure("inactive")
    revoked: Final = await _delete_sessions(writer, auth.user_id, auth.token, others=False)
    if revoked:
        await append_native_audit(
            writer,
            actor_id=auth.user_id,
            action="native_session.revoked",
            affected_count=len(revoked),
        )
    return NativeSessionSuccess(bool(revoked), revoked)
