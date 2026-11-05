import asyncio
import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

from prisma.errors import PrismaError
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from litellm.constants import (
    LITELLM_INTERNAL_JOBS_SERVICE_ACCOUNT_NAME,
    LITELLM_PROXY_ADMIN_NAME,
    LITTELM_INTERNAL_HEALTH_SERVICE_ACCOUNT_NAME,
)
from litellm.proxy._types import LiteLLM_UserTable, LitellmUserRoles, UserAPIKeyAuth

from .api_jwt_config import ApiJwtBinding, ApiJwtConfig, ApiJwtFailure
from .api_jwt_provider import VerifiedApiJwt
from .control_plane_flags import native_audit_enabled
from .native_audit_store import (
    NativeAuditClient,
    NativeAuditConflict,
    NativeAuditTransaction,
    NativeAuditUnavailable,
    NativeAuditWriter,
    append_native_audit,
)

_SLOTS: Final = asyncio.Semaphore(4)
_ROLES: Final = frozenset({"customer", "internal_user"})
_KEY_GRAPH: Final = frozenset({"team_id", "organization_id", "project_id", "object_permission_id", "agent_id"})
_USER_GRAPH: Final = frozenset({"team_id", "organization_id", "object_permission_id"})
_RESERVED_USERS: Final = frozenset({
    LITELLM_PROXY_ADMIN_NAME, LITELLM_INTERNAL_JOBS_SERVICE_ACCOUNT_NAME,
    LITTELM_INTERNAL_HEALTH_SERVICE_ACCOUNT_NAME, "__db_unavailable_fallback__",
})
_MODEL_ROUTES: Final = frozenset({
    "llm_api_routes", "/chat/completions", "/v1/chat/completions", "/messages", "/v1/messages",
    "/responses", "/v1/responses",
})


@dataclass(frozen=True, slots=True)
class ResolvedApiJwt:
    auth: UserAPIKeyAuth = field(repr=False)
    user: LiteLLM_UserTable = field(repr=False)
    target_hash: str = field(repr=False)


class _Authority(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    namespace: str
    generation: int
    digest: str


class _Binding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: str
    token_hash: str
    revision: UUID
    enabled: bool


class _Target(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    key_data: Mapping[str, JsonValue]
    user_data: Mapping[str, JsonValue]
    active: bool


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    total: int


_AUTHORITIES: Final = TypeAdapter(tuple[_Authority, ...])
_BINDINGS: Final = TypeAdapter(tuple[_Binding, ...])
_TARGETS: Final = TypeAdapter(tuple[_Target, ...])
_COUNTS: Final = TypeAdapter(tuple[_Count, ...])


def _metadata(value: JsonValue | None) -> Mapping[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _subject_hash(subject: str) -> str:
    return hashlib.sha256(subject.encode()).hexdigest()


def _ordinary_routes(value: JsonValue | None) -> bool:
    return value is None or isinstance(value, list) and all(
        isinstance(route, str) and route in _MODEL_ROUTES for route in value
    )


def _eligible(target: _Target, binding: ApiJwtBinding) -> bool:
    key: Final = target.key_data
    user: Final = target.user_data
    metadata: Final = _metadata(key.get("metadata"))
    user_metadata: Final = _metadata(user.get("metadata"))
    return bool(
        target.active and key.get("token") == binding.token_hash
        and key.get("user_id") == binding.user_id and user.get("user_id") == binding.user_id
        and binding.user_id not in _RESERVED_USERS
        and isinstance(user.get("user_role"), str) and user.get("user_role") in _ROLES
        and user.get("password_reset_required") is not True
        and user_metadata.get("scim_active") is not False
        and key.get("key_type") in (None, "llm_api") and _ordinary_routes(key.get("allowed_routes"))
        and not any(key.get(name) is not None for name in _KEY_GRAPH)
        and not any(user.get(name) is not None for name in _USER_GRAPH)
        and not user.get("teams")
        and metadata.get("login_method") is None and metadata.get("service_account_id") is None
        and not metadata.get("service_account") and not metadata.get("managed_agent")
        and not key.get("access_group_ids") and not key.get("object_permission")
    )


def _same_authority(current: _Authority, config: ApiJwtConfig) -> bool:
    return (current.namespace, current.generation, current.digest) == (
        config.namespace, config.generation, config.digest(),
    )


def _same_binding(current: _Binding, binding: ApiJwtBinding) -> bool:
    return (current.user_id, current.token_hash, current.revision, current.enabled) == (
        binding.user_id, binding.token_hash, binding.binding_revision, binding.enabled,
    )


async def _target(writer: NativeAuditWriter, token_hash: str, user_id: str) -> _Target | None:
    rows: Final = _TARGETS.validate_python(await writer.query_raw(
        '''SELECT to_jsonb(k) AS key_data,to_jsonb(u)-'password'-'last_breach_check_at' AS user_data,
        (k.blocked IS DISTINCT FROM true AND
        (k.expires IS NULL OR (k.expires AT TIME ZONE 'UTC')>clock_timestamp())) AS active
        FROM "LiteLLM_VerificationToken" k JOIN "LiteLLM_UserTable" u ON u.user_id=k.user_id
        WHERE k.token=$1 AND k.user_id=$2''', token_hash, user_id,
    ))
    return rows[0] if rows else None


async def _binding(writer: NativeAuditWriter, config: ApiJwtConfig, subject: str) -> _Binding | None:
    rows: Final = _BINDINGS.validate_python(await writer.query_raw(
        '''SELECT user_id,token_hash,revision,enabled FROM retail_api_jwt_bindings
        WHERE namespace=$1 AND issuer=$2 AND subject_hash=$3''', config.namespace, config.issuer, _subject_hash(subject),
    ))
    return rows[0] if rows else None


async def _configure_binding(
    writer: NativeAuditTransaction, config: ApiJwtConfig, binding: ApiJwtBinding,
) -> ApiJwtFailure | None:
    existing: Final = await _binding(writer, config, binding.subject)
    if existing is not None and _same_binding(existing, binding):
        return None
    if binding.enabled:
        target: Final = await _target(writer, binding.token_hash, binding.user_id)
        if target is None or not _eligible(target, binding):
            return ApiJwtFailure("invalid")
    used: Final = _COUNTS.validate_python(await writer.query_raw(
        "SELECT count(*) AS total FROM retail_api_jwt_revisions WHERE revision=$1::uuid",
        str(binding.binding_revision),
    ))[0].total
    count: Final = _COUNTS.validate_python(await writer.query_raw(
        "SELECT count(*) AS total FROM (SELECT 1 FROM retail_api_jwt_revisions LIMIT 10000) q",
    ))[0].total
    if used:
        return ApiJwtFailure("invalid")
    if count >= 10000:
        return ApiJwtFailure("unavailable")
    await writer.query_raw(
        "INSERT INTO retail_api_jwt_revisions(revision) VALUES($1::uuid) RETURNING revision",
        str(binding.binding_revision),
    )
    await writer.query_raw(
        '''INSERT INTO retail_api_jwt_bindings(namespace,issuer,subject_hash,user_id,token_hash,revision,enabled)
        VALUES($1,$2,$3,$4,$5,$6::uuid,$7) ON CONFLICT(namespace,issuer,subject_hash) DO UPDATE SET
        user_id=EXCLUDED.user_id,token_hash=EXCLUDED.token_hash,revision=EXCLUDED.revision,enabled=EXCLUDED.enabled
        RETURNING revision''', config.namespace, config.issuer, _subject_hash(binding.subject), binding.user_id,
        binding.token_hash, str(binding.binding_revision), binding.enabled,
    )
    return None


async def _configure(writer: NativeAuditTransaction, config: ApiJwtConfig) -> ApiJwtFailure | None:
    await writer.query_raw("SELECT pg_advisory_xact_lock(799405122)::text")
    rows: Final = _AUTHORITIES.validate_python(await writer.query_raw(
        "SELECT namespace,generation,digest FROM retail_api_jwt_settings WHERE singleton FOR UPDATE",
    ))
    if rows and _same_authority(rows[0], config):
        return None
    if rows and config.generation <= rows[0].generation:
        return ApiJwtFailure("invalid")
    for binding in config.bindings:
        failure: Final = await _configure_binding(writer, config, binding)
        if failure is not None:
            return failure
    await writer.query_raw(
        '''UPDATE retail_api_jwt_bindings SET enabled=false WHERE enabled
        AND (namespace<>$1 OR issuer<>$2 OR NOT(subject_hash=ANY($3::text[]))) RETURNING revision''',
        config.namespace, config.issuer, list(_subject_hash(binding.subject) for binding in config.bindings),
    )
    await writer.query_raw(
        '''INSERT INTO retail_api_jwt_settings(singleton,namespace,generation,digest)
        VALUES(true,$1,$2,$3) ON CONFLICT(singleton) DO UPDATE SET namespace=EXCLUDED.namespace,
        generation=EXCLUDED.generation,digest=EXCLUDED.digest,updated_at=clock_timestamp() RETURNING singleton''',
        config.namespace, config.generation, config.digest(),
    )
    await append_native_audit(
        writer, actor_id="system:api-identity-configuration", action="native_api_identity.configured",
    )
    return None


async def _fresh(
    writer: NativeAuditWriter, config: ApiJwtConfig, verified: VerifiedApiJwt,
) -> ResolvedApiJwt | ApiJwtFailure:
    rows: Final = _TARGETS.validate_python(await writer.query_raw(
        '''SELECT to_jsonb(k) AS key_data,to_jsonb(u)-'password'-'last_breach_check_at' AS user_data,
        (k.blocked IS DISTINCT FROM true AND
        (k.expires IS NULL OR (k.expires AT TIME ZONE 'UTC')>clock_timestamp())) AS active
        FROM retail_api_jwt_settings s JOIN retail_api_jwt_bindings b
        ON b.namespace=s.namespace AND b.issuer=$4 AND b.subject_hash=$5 AND b.revision=$6::uuid AND b.enabled
        JOIN "LiteLLM_VerificationToken" k ON k.token=b.token_hash AND k.user_id=b.user_id
        JOIN "LiteLLM_UserTable" u ON u.user_id=b.user_id
        WHERE s.singleton AND s.namespace=$1 AND s.generation=$2 AND s.digest=$3
        AND $7::timestamptz>clock_timestamp()''',
        config.namespace, config.generation, config.digest(), config.issuer,
        _subject_hash(verified.subject.get_secret_value()), str(verified.binding_revision), verified.expires_at,
    ))
    expected: Final = next((row for row in config.bindings if row.subject == verified.subject.get_secret_value()), None)
    if (
        expected is None or not expected.enabled or expected.binding_revision != verified.binding_revision
        or verified.client_id.get_secret_value() != config.client_id
        or not rows or not _eligible(rows[0], expected)
    ):
        return ApiJwtFailure("invalid")
    user: Final = LiteLLM_UserTable.model_validate(rows[0].user_data)
    auth: Final = UserAPIKeyAuth.model_validate(rows[0].key_data).model_copy(update={
        "api_key": expected.token_hash, "token": expected.token_hash, "user_id": expected.user_id,
        "user_role": LitellmUserRoles(user.user_role), "jwt_claims": None, "via_virtual_key": True,
    })
    return ResolvedApiJwt(auth, user, expected.token_hash)


class _AbortConfiguration(Exception):
    def __init__(self, failure: ApiJwtFailure) -> None:
        self.failure = failure


class NativeApiJwtStore:
    def __init__(self, native_client: NativeAuditClient) -> None:
        self._client = native_client
        self._closed = False

    @asynccontextmanager
    async def _transaction(self, timeout: float) -> AsyncIterator[NativeAuditTransaction]:
        async with asyncio.timeout(timeout), _SLOTS, self._client.tx(timeout=timedelta(seconds=timeout)) as writer:
            await writer.execute_raw("SET LOCAL lock_timeout='500ms'")
            await writer.execute_raw("SET LOCAL statement_timeout='1500ms'")
            yield writer

    async def initialize(self) -> ApiJwtFailure | None:
        if self._closed:
            return ApiJwtFailure("unavailable")
        try:
            async with self._transaction(8) as writer:
                await writer.execute_raw(Path(__file__).with_name("api_jwt_schema.sql").read_text(encoding="utf-8"))
        except (PrismaError, TimeoutError, ValidationError):
            return ApiJwtFailure("unavailable")
        return None

    async def configure(self, config: ApiJwtConfig) -> ApiJwtFailure | None:
        if self._closed or not native_audit_enabled():
            return ApiJwtFailure("unavailable")
        try:
            async with self._transaction(8) as writer:
                failure: Final = await _configure(writer, config)
                if failure is not None:
                    raise _AbortConfiguration(failure)
        except _AbortConfiguration as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError, NativeAuditUnavailable, NativeAuditConflict):
            return ApiJwtFailure("unavailable")
        return None

    async def fresh(self, config: ApiJwtConfig, verified: VerifiedApiJwt) -> ResolvedApiJwt | ApiJwtFailure:
        if self._closed:
            return ApiJwtFailure("unavailable")
        try:
            async with self._transaction(2) as writer:
                return await _fresh(writer, config, verified)
        except (PrismaError, TimeoutError, ValidationError):
            return ApiJwtFailure("unavailable")

    async def close(self) -> None:
        self._closed = True
