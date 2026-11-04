import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, NoReturn
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from prisma.errors import PrismaError
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth, hash_token
from litellm.proxy.auth.route_checks import RouteChecks

from .control_plane_flags import native_audit_enabled
from .native_audit_store import (
    NativeAuditClient,
    NativeAuditConflict,
    NativeAuditTransaction,
    NativeAuditUnavailable,
    NativeAuditWriter,
    append_native_audit,
)

OIDC_SESSION_PREFIX: Final = "sk-retail-oidc-ui-"
OIDC_SESSION_METHOD: Final = "retail_oidc"
OIDC_SESSION_ROUTES: Final = ("management_routes", "info_routes", "/session/logout", "/retail/*")
OIDC_NATIVE_ROLES: Final = frozenset({
    "proxy_admin", "proxy_admin_viewer", "internal_user", "internal_user_viewer", "customer",
})
_SLOTS: Final = asyncio.Semaphore(2)


class BindingSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    subject: str = Field(min_length=1, max_length=255, repr=False)
    user_id: str = Field(min_length=1, max_length=191, repr=False)
    enabled: StrictBool = True


class SessionSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    namespace: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    generation: StrictInt = Field(ge=1, le=9_223_372_036_854_775_807)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    issuer: str = Field(min_length=1, max_length=2048, repr=False)
    session_seconds: StrictInt = Field(ge=300, le=3600)
    bindings: tuple[BindingSpec, ...] = Field(max_length=128, repr=False)

    @model_validator(mode="after")
    def unique_subjects(self) -> "SessionSettings":
        if len(frozenset(row.subject for row in self.bindings)) != len(self.bindings):
            raise ValueError("OIDC subjects must have unique explicit bindings")
        return self


class MintedOIDCSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    jwt_token: str = Field(repr=False)
    expires_at: AwareDatetime


class _Authority(BaseModel):
    model_config = ConfigDict(frozen=True)
    namespace: str
    generation: int
    digest: str
    issuer: str
    session_seconds: int


class _Binding(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    revision: UUID
    user_id: str
    enabled: bool


class _User(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: str
    user_role: str | None
    user_email: str | None = None
    password_reset_required: bool | None


class _FreshSession(_User):
    model_config = ConfigDict(frozen=True)
    expires_at: AwareDatetime
    session_id: UUID
    metadata: Mapping[str, JsonValue]
    allowed_routes: tuple[str, ...]


class _Expiry(BaseModel):
    model_config = ConfigDict(frozen=True)
    expires_at: AwareDatetime


class _Capacity(BaseModel):
    model_config = ConfigDict(frozen=True)
    full: bool


@dataclass(frozen=True, slots=True)
class _Failure:
    reason: Literal["inactive", "configuration_conflict", "binding_conflict", "capacity"]


_AUTHORITIES: Final = TypeAdapter(tuple[_Authority, ...])
_BINDINGS: Final = TypeAdapter(tuple[_Binding, ...])
_USERS: Final = TypeAdapter(tuple[_User, ...])
_FRESH: Final = TypeAdapter(tuple[_FreshSession, ...])
_EXPIRY: Final = TypeAdapter(tuple[_Expiry, ...])
_CAPACITY: Final = TypeAdapter(tuple[_Capacity, ...])


def _require(outcome: _Failure | None) -> None:
    if outcome is None:
        return
    _raise_failure(outcome)


def _raise_failure(outcome: _Failure) -> NoReturn:
    match outcome.reason:
        case "inactive":
            raise HTTPException(401, "This sign-in session is unavailable")
        case "configuration_conflict" | "binding_conflict":
            raise HTTPException(409, "The sign-in authority configuration changed")
        case "capacity":
            raise HTTPException(503, "Sign-in capacity is temporarily unavailable")


def oidc_raw_key(value: str | None) -> bool:
    return isinstance(value, str) and value.removeprefix("Bearer ").startswith(OIDC_SESSION_PREFIX)


def oidc_request_key(request: Request, fallback: str | None) -> str | None:
    match getattr(request, "scope", None):
        case {"retail_oidc_credential": str() as key}:
            return key
        case _:
            return fallback


def remember_oidc_credential(request: Request, key: str | None, route: str) -> None:
    if not oidc_raw_key(key):
        return
    if not oidc_control_plane_route(route, request.method):
        raise HTTPException(403, "This sign-in session is limited to the control plane")
    request.scope["retail_oidc_credential"] = key  # rebind-ok: carry the normalized credential past legacy auth returns


def oidc_key_metadata(value: object) -> bool:
    match value:
        case {"login_method": "retail_oidc"}:
            return True
        case _:
            return False


def reject_oidc_key_regeneration(metadata: object, key: str = "") -> None:
    if oidc_raw_key(key) or oidc_key_metadata(metadata):
        raise HTTPException(403, "Sign-in sessions must be replaced by signing in again")


def reject_oidc_key_update(metadata: object, fields: frozenset[str]) -> None:
    if oidc_key_metadata(metadata) and not fields.issubset({"key", "blocked"}):
        raise HTTPException(403, "Sign-in session identity and route scope cannot be changed")


def oidc_control_plane_route(route: str, method: str) -> bool:
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        return False
    if route.lower().startswith(("/mcp", "/v1/mcp", "/byok", "/experimental", "/realtime", "/v1/realtime")):
        return False
    if RouteChecks.is_llm_api_route(route):
        return False
    return (
        route.startswith("/retail/") and not route.startswith("/retail/auth/")
    ) or route == "/session/logout" or RouteChecks.is_management_route(route) or RouteChecks.is_info_route(route)


def _valid_user(user: _User | None) -> bool:
    return bool(user and user.user_role in OIDC_NATIVE_ROLES and user.password_reset_required is not True)


def _same_authority(row: _Authority, settings: SessionSettings) -> bool:
    return (
        row.namespace, row.generation, row.digest, row.issuer, row.session_seconds,
    ) == (
        settings.namespace, settings.generation, settings.digest, settings.issuer, settings.session_seconds,
    )


async def _read_authority(writer: NativeAuditWriter, *, lock: bool = False) -> _Authority | None:
    rows: Final = _AUTHORITIES.validate_python(await writer.query_raw(
        "SELECT namespace,generation,digest,issuer,session_seconds FROM retail_oidc_settings WHERE singleton"
        + (" FOR UPDATE" if lock else ""),
    ))
    return rows[0] if rows else None


async def _configure_binding(
    writer: NativeAuditTransaction, settings: SessionSettings, spec: BindingSpec,
) -> _Failure | None:
    rows: Final = _BINDINGS.validate_python(await writer.query_raw(
        """SELECT id,revision,user_id,enabled FROM retail_oidc_bindings
        WHERE namespace=$1 AND issuer=$2 AND subject=$3 FOR UPDATE""",
        settings.namespace, settings.issuer, spec.subject,
    ))
    if rows and rows[0].user_id != spec.user_id:
        return _Failure("binding_conflict")
    users: Final = _USERS.validate_python(await writer.query_raw(
        'SELECT user_id,user_role,user_email,password_reset_required FROM "LiteLLM_UserTable" WHERE user_id=$1 FOR SHARE',
        spec.user_id,
    ))
    if spec.enabled and not _valid_user(users[0] if users else None):
        return _Failure("inactive")
    await writer.query_raw(
        """INSERT INTO retail_oidc_bindings(namespace,issuer,subject,user_id,enabled)
        VALUES($1,$2,$3,$4,$5) ON CONFLICT(namespace,issuer,subject) DO UPDATE
        SET enabled=EXCLUDED.enabled WHERE retail_oidc_bindings.user_id=EXCLUDED.user_id RETURNING id""",
        settings.namespace, settings.issuer, spec.subject, spec.user_id, spec.enabled,
    )
    return None


async def _configure(writer: NativeAuditTransaction, settings: SessionSettings) -> _Failure | None:
    await writer.query_raw("SELECT pg_advisory_xact_lock(799404111)::text")
    current: Final = await _read_authority(writer, lock=True)
    if current is not None and _same_authority(current, settings):
        return None
    if current is not None and settings.generation <= current.generation:
        return _Failure("configuration_conflict")
    for spec in settings.bindings:
        outcome: Final = await _configure_binding(writer, settings, spec)
        if outcome is not None:
            return outcome
    await writer.query_raw(
        """UPDATE retail_oidc_bindings SET enabled=false WHERE enabled
        AND (namespace<>$1 OR issuer<>$2 OR NOT(subject=ANY($3::text[]))) RETURNING id""",
        settings.namespace, settings.issuer, list(row.subject for row in settings.bindings),
    )
    await writer.query_raw(
        """INSERT INTO retail_oidc_settings(singleton,namespace,generation,digest,issuer,session_seconds)
        VALUES(true,$1,$2,$3,$4,$5) ON CONFLICT(singleton) DO UPDATE SET
        namespace=EXCLUDED.namespace,generation=EXCLUDED.generation,digest=EXCLUDED.digest,
        issuer=EXCLUDED.issuer,session_seconds=EXCLUDED.session_seconds,updated_at=clock_timestamp() RETURNING singleton""",
        settings.namespace, settings.generation, settings.digest, settings.issuer, settings.session_seconds,
    )
    await append_native_audit(writer, actor_id="system:oidc-configuration", action="native_identity.configured")
    return None


async def _mint_user(
    writer: NativeAuditTransaction, settings: SessionSettings, subject: str,
) -> tuple[_Binding, _User] | _Failure:
    authority: Final = await _read_authority(writer, lock=True)
    if authority is None or not _same_authority(authority, settings):
        return _Failure("configuration_conflict")
    bindings: Final = _BINDINGS.validate_python(await writer.query_raw(
        """SELECT id,revision,user_id,enabled FROM retail_oidc_bindings
        WHERE namespace=$1 AND issuer=$2 AND subject=$3 FOR UPDATE""",
        settings.namespace, settings.issuer, subject,
    ))
    if not bindings or not bindings[0].enabled:
        return _Failure("inactive")
    binding: Final = bindings[0]
    users: Final = _USERS.validate_python(await writer.query_raw(
        'SELECT user_id,user_role,user_email,password_reset_required FROM "LiteLLM_UserTable" WHERE user_id=$1 FOR UPDATE',
        binding.user_id,
    ))
    if not users or not _valid_user(users[0]):
        return _Failure("inactive")
    await writer.query_raw(
        """DELETE FROM retail_oidc_sessions WHERE token_hash IN (
        SELECT token_hash FROM retail_oidc_sessions WHERE expires_at<=clock_timestamp()
        ORDER BY expires_at,token_hash LIMIT 128) RETURNING id""",
    )
    capacity: Final = _CAPACITY.validate_python(await writer.query_raw(
        """SELECT ((SELECT count(*) FROM (SELECT 1 FROM retail_oidc_sessions LIMIT 10000) q)>=10000
        OR (SELECT count(*) FROM (SELECT 1 FROM retail_oidc_sessions
        WHERE user_id=$1 AND expires_at>clock_timestamp() LIMIT 128) q)>=128) AS full""",
        binding.user_id,
    ))
    if capacity[0].full:
        return _Failure("capacity")
    return binding, users[0]


async def _insert_session(
    writer: NativeAuditTransaction, settings: SessionSettings, binding: _Binding, user: _User, key: str,
) -> datetime:
    identifier: Final = uuid4()
    metadata: Final = json.dumps({"login_method": OIDC_SESSION_METHOD, "oidc_session_id": str(identifier)})
    expiry: Final = _EXPIRY.validate_python(await writer.query_raw(
        "SELECT date_trunc('milliseconds',clock_timestamp())+($1::integer*interval '1 second') AS expires_at",
        settings.session_seconds,
    ))[0].expires_at
    await writer.execute_raw("SET LOCAL retail.oidc_mint='on'")
    await writer.query_raw(
        """INSERT INTO "LiteLLM_VerificationToken"
        (token,user_id,team_id,metadata,expires,models,allowed_routes,key_type,created_by,updated_by)
        VALUES($1,$2,$3,$4::jsonb,($5::timestamptz AT TIME ZONE 'UTC'),
        ARRAY[]::text[],$6::text[],'management',$2,$2) RETURNING token""",
        hash_token(key), user.user_id, UI_SESSION_TOKEN_TEAM_ID, metadata, expiry,
        list(OIDC_SESSION_ROUTES),
    )
    await writer.query_raw(
        """INSERT INTO retail_oidc_sessions
        (token_hash,id,namespace,generation,digest,binding_id,binding_revision,user_id,expires_at)
        VALUES($1,$2::uuid,$3,$4,$5,$6::uuid,$7::uuid,$8,$9::timestamptz) RETURNING id""",
        hash_token(key), str(identifier), settings.namespace, settings.generation, settings.digest,
        str(binding.id), str(binding.revision), user.user_id, expiry,
    )
    await append_native_audit(writer, actor_id=user.user_id, action="native_identity.signed_in")
    return expiry


class NativeOIDCSessionStore:
    def __init__(self, native_client: NativeAuditClient) -> None:
        self._client = native_client
        self._slots = _SLOTS
        self._closed = False

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[NativeAuditTransaction]:
        if self._closed:
            raise HTTPException(503, "Sign-in storage is temporarily unavailable")
        try:
            async with asyncio.timeout(8), self._slots, self._client.tx(timeout=timedelta(seconds=6)) as writer:
                await writer.execute_raw("SET LOCAL lock_timeout='1000ms'")
                await writer.execute_raw("SET LOCAL statement_timeout='4000ms'")
                yield writer
        except (PrismaError, TimeoutError, NativeAuditUnavailable, NativeAuditConflict, ValidationError) as exc:
            raise HTTPException(503, "Sign-in storage is temporarily unavailable") from exc

    async def initialize(self) -> None:
        schema: Final = Path(__file__).with_name("oidc_session_schema.sql").read_text(encoding="utf-8")
        async with self._transaction() as writer:
            await writer.execute_raw(schema)

    async def configure(self, settings: SessionSettings) -> None:
        if not native_audit_enabled():
            raise HTTPException(503, "Sign-in audit storage is temporarily unavailable")
        async with self._transaction() as writer:
            _require(await _configure(writer, settings))

    async def mint(
        self, settings: SessionSettings, subject: str, *, general_settings: Mapping[str, JsonValue],
        premium_user: bool, master_key: str,
    ) -> MintedOIDCSession:
        from litellm.proxy.auth.login_utils import LoginResult, create_ui_token_object, encode_ui_session_jwt

        if not native_audit_enabled() or not master_key:
            raise HTTPException(503, "Sign-in audit storage is temporarily unavailable")
        async with self._transaction() as writer:
            selected: Final = await _mint_user(writer, settings, subject)
            if isinstance(selected, _Failure):
                _raise_failure(selected)
            binding, user = selected
            key: Final = OIDC_SESSION_PREFIX + secrets.token_urlsafe(32)
            expires: Final = await _insert_session(writer, settings, binding, user, key)
            login: Final = LoginResult(
                user_id=user.user_id, key=key, user_email=user.user_email,
                user_role=user.user_role or "", login_method="sso", password_reset_required=False,
            )
            payload: Final = create_ui_token_object(login, dict(general_settings), premium_user)
            encoded: Final = encode_ui_session_jwt(payload, master_key, expires_at=expires)
        return MintedOIDCSession(jwt_token=encoded, expires_at=expires)

    async def close(self) -> None:
        self._closed = True


async def _fresh_session(reader: NativeAuditWriter, token: str) -> _FreshSession | None:
    rows: Final = _FRESH.validate_python(await reader.query_raw(
        """SELECT u.user_id,u.user_role,u.user_email,u.password_reset_required,
        p.expires_at,p.id AS session_id,k.metadata,k.allowed_routes
        FROM retail_oidc_sessions p JOIN retail_oidc_settings s ON s.singleton
        AND s.namespace=p.namespace AND s.generation=p.generation AND s.digest=p.digest
        JOIN retail_oidc_bindings b ON b.id=p.binding_id AND b.revision=p.binding_revision
        AND b.enabled AND b.user_id=p.user_id AND b.namespace=p.namespace AND b.issuer=s.issuer
        JOIN "LiteLLM_UserTable" u ON u.user_id=p.user_id
        JOIN "LiteLLM_VerificationToken" k ON k.token=p.token_hash AND k.user_id=p.user_id
        WHERE p.token_hash=$1 AND p.expires_at>clock_timestamp()
        AND (k.expires AT TIME ZONE 'UTC')=p.expires_at
        AND k.team_id=$2 AND k.blocked IS DISTINCT FROM true
        AND k.metadata->>'login_method'='retail_oidc'
        AND k.metadata->>'oidc_session_id'=p.id::text AND k.allowed_routes=$3::text[]""",
        token, UI_SESSION_TOKEN_TEAM_ID, list(OIDC_SESSION_ROUTES),
    ))
    return rows[0] if rows else None


async def fresh_oidc_session(
    auth: UserAPIKeyAuth, raw_key: str | None, route: str, method: str,
    *, reader: NativeAuditWriter | None = None,
) -> UserAPIKeyAuth:
    if not oidc_raw_key(raw_key) and not oidc_key_metadata(auth.metadata):
        return auth
    if not oidc_control_plane_route(route, method):
        raise HTTPException(403, "This sign-in session is limited to the control plane")
    if not oidc_raw_key(raw_key):
        raise HTTPException(401, "This sign-in session is unavailable")
    key: Final = (raw_key or "").removeprefix("Bearer ")
    source: Final = reader if reader is not None else _native_reader()
    try:
        async with asyncio.timeout(4), _SLOTS:
            fresh: Final = await _fresh_session(source, hash_token(key))
    except (PrismaError, TimeoutError, ValidationError) as exc:
        raise HTTPException(503, "Account authority is temporarily unavailable") from exc
    if not _valid_user(fresh) or fresh is None or fresh.user_id != auth.user_id:
        raise HTTPException(401, "This sign-in session is unavailable")
    return auth.model_copy(update={
        "user_role": LitellmUserRoles(fresh.user_role), "team_id": UI_SESSION_TOKEN_TEAM_ID,
        "metadata": dict(fresh.metadata), "allowed_routes": list(fresh.allowed_routes),
        "expires": fresh.expires_at, "blocked": False,
    })


def _native_reader() -> NativeAuditWriter:
    from litellm.proxy import proxy_server

    if proxy_server.prisma_client is None:
        raise HTTPException(503, "Account authority is temporarily unavailable")
    return proxy_server.prisma_client.writer_db
