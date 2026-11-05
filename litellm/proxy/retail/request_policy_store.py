import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Literal, Protocol, TypeAlias
from uuid import UUID, uuid4

from fastapi import HTTPException
from prisma.errors import PrismaError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from litellm.constants import LITELLM_PROXY_MASTER_KEY_ALIAS
from litellm.proxy._types import LiteLLM_UserTable, LitellmUserRoles, UserAPIKeyAuth
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
from .object_metadata_targets import safe_display_name
from .request_policy_management import (
    BindingCreate,
    BindingRow,
    BindingUpdate,
    PolicyActor,
    PolicyCreate,
    PolicyRow,
    PolicyTarget,
    PolicyUpdate,
    PromptCreate,
    PromptRow,
    PromptUpdate,
    ResourceKind,
    TargetKind,
)
from .request_policy_types import ApplicablePolicy, Definition, RequestPolicyFailure, ResolvedRequestPolicies

Reply: TypeAlias = Mapping[str, JsonValue] | RequestPolicyFailure
WriteInput: TypeAlias = PromptCreate | PromptUpdate | PolicyCreate | PolicyUpdate | BindingCreate | BindingUpdate
Resource: TypeAlias = PromptRow | PolicyRow | BindingRow
_SLOTS: Final = asyncio.Semaphore(4)
_JSON: Final = TypeAdapter(dict[str, JsonValue])
_HASH: Final = re.compile(r"[a-f0-9]{64}\Z")
_TABLES: Final = {"prompts": "retail_request_policy_prompts", "policies": "retail_request_policy_policies", "bindings": "retail_request_policy_bindings"}


class RequestPolicyClient(NativeAuditClient, Protocol):
    @property
    def writer_db(self) -> NativeAuditWriter: ...


class _Abort(Exception):
    def __init__(self, code: Literal["forbidden", "conflict", "unavailable", "not_found", "invalid", "capacity"]) -> None:
        self.failure = RequestPolicyFailure(code)


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    total: int


class _Admin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    user_id: str = Field(repr=False)
    user_role: str | None
    password_reset_required: bool | None
    created_at: datetime | None = Field(repr=False)
    inactive: bool


class _Credential(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    allowed_routes: tuple[str, ...] | None
    team_id: str | None


class _Target(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    id: UUID
    kind: TargetKind
    native_id: str = Field(repr=False)
    anchor: str = Field(repr=False)
    native_created_at: AwareDatetime | None
    owner_id: str | None = Field(repr=False)
    owner_created_at: AwareDatetime | None = Field(repr=False)
    label: str


class _NativeTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    native_id: str = Field(repr=False)
    native_created_at: AwareDatetime | None
    owner_id: str | None = Field(repr=False)
    owner_created_at: AwareDatetime | None = Field(repr=False)
    label: str | None


class _Receipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    digest: str
    response: Mapping[str, JsonValue] = Field(repr=False)


class _Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    target_kind: TargetKind
    policy_id: UUID
    policy_version: int
    definition: Definition = Field(repr=False)
    prompt_id: UUID | None
    prompt_version: int | None
    prompt_text: str | None = Field(repr=False)
    valid: bool


class _Lookup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    key_data: Mapping[str, JsonValue] | None = Field(repr=False)
    user_data: Mapping[str, JsonValue] | None = Field(repr=False)
    active: bool
    rules: tuple[_Rule, ...] = Field(repr=False)


_COUNTS: Final = TypeAdapter(tuple[_Count, ...])
_ADMINS: Final = TypeAdapter(tuple[_Admin, ...])
_TARGETS: Final = TypeAdapter(tuple[_Target, ...])
_NATIVES: Final = TypeAdapter(tuple[_NativeTarget, ...])
_RECEIPTS: Final = TypeAdapter(tuple[_Receipt, ...])
_LOOKUPS: Final = TypeAdapter(tuple[_Lookup, ...])


def _json(value: BaseModel) -> Mapping[str, JsonValue]:
    return _JSON.validate_python(value.model_dump(mode="json"))


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _anchor(target: _NativeTarget) -> str:
    return _digest({"created_at": target.native_created_at.astimezone(timezone.utc).isoformat() if target.native_created_at else None,
        "owner_id": target.owner_id, "owner_created_at": target.owner_created_at.astimezone(timezone.utc).isoformat() if target.owner_created_at else None})


async def _admin(writer: NativeAuditTransaction, actor: PolicyActor) -> str:
    rows: Final = _ADMINS.validate_python(await writer.query_raw(
        '''SELECT user_id,user_role,password_reset_required,created_at,
        coalesce(metadata->'scim_active'='false'::jsonb,false) AS inactive FROM "LiteLLM_UserTable" WHERE user_id=$1 FOR SHARE''',
        actor.user_id,
    ))
    if actor.trusted_master:
        if rows and (rows[0].user_role != "proxy_admin" or rows[0].password_reset_required or rows[0].inactive):
            raise _Abort("forbidden")
        return rows[0].user_id if rows else "system:native-master"
    if not rows or rows[0].user_role != "proxy_admin" or rows[0].password_reset_required or rows[0].inactive:
        raise _Abort("forbidden")
    if not actor.token_hash or not _HASH.fullmatch(actor.token_hash):
        raise _Abort("forbidden")
    credentials: Final = TypeAdapter(tuple[_Credential, ...]).validate_python(await writer.query_raw(
        '''SELECT allowed_routes,team_id FROM "LiteLLM_VerificationToken" WHERE token=$1 AND user_id=$2
        AND blocked IS DISTINCT FROM true AND (expires IS NULL OR (expires AT TIME ZONE 'UTC')>clock_timestamp()) FOR SHARE''',
        actor.token_hash, actor.user_id,
    ))
    if not credentials:
        raise _Abort("forbidden")
    credential: Final = credentials[0]
    try:
        RouteChecks.is_virtual_key_allowed_to_call_route(route=actor.request_route, valid_token=UserAPIKeyAuth(
            user_id=actor.user_id,user_role=LitellmUserRoles.PROXY_ADMIN,
            allowed_routes=list(credential.allowed_routes) if credential.allowed_routes is not None else None,
            team_id=credential.team_id,
        ))
    except HTTPException as exc:
        raise _Abort("forbidden") from exc
    return rows[0].user_id


async def _write_lock(writer: NativeAuditTransaction) -> None:
    if not native_audit_enabled():
        raise _Abort("unavailable")
    await writer.query_raw("SELECT pg_advisory_xact_lock(799405152)::text")


async def _capacity(writer: NativeAuditTransaction, kind: Literal["prompt", "policy", "target", "binding", "receipt"]) -> None:
    maximum: Final = 1000 if kind in {"prompt", "policy"} else 10000
    rows: Final = _COUNTS.validate_python(await writer.query_raw(
        f"SELECT {kind}_count AS total FROM retail_request_policy_settings WHERE singleton",
    ))
    if not rows or rows[0].total >= maximum:
        raise _Abort("capacity")
    await writer.query_raw(
        f"UPDATE retail_request_policy_settings SET {kind}_count={kind}_count+1 WHERE singleton RETURNING singleton",
    )


async def _native(writer: NativeAuditTransaction, kind: TargetKind, native_id: str) -> _NativeTarget | None:
    if kind == "user":
        query: Final = '''SELECT user_id AS native_id,created_at AT TIME ZONE 'UTC' AS native_created_at,
        NULL::text AS owner_id,NULL::timestamptz AS owner_created_at,user_alias AS label
        FROM "LiteLLM_UserTable" WHERE user_id=$1 AND created_at IS NOT NULL'''
    else:
        query: Final = '''SELECT k.token AS native_id,k.created_at AT TIME ZONE 'UTC' AS native_created_at,
        k.user_id AS owner_id,u.created_at AT TIME ZONE 'UTC' AS owner_created_at,k.key_alias AS label
        FROM "LiteLLM_VerificationToken" k LEFT JOIN "LiteLLM_UserTable" u ON u.user_id=k.user_id
        WHERE k.token=$1 AND k.created_at IS NOT NULL AND (k.user_id IS NULL OR u.created_at IS NOT NULL)'''
    rows: Final = _NATIVES.validate_python(await writer.query_raw(query, native_id))
    return rows[0] if rows else None


async def _target(writer: NativeAuditTransaction, identifier: UUID) -> _Target:
    rows: Final = _TARGETS.validate_python(await writer.query_raw(
        "SELECT * FROM retail_request_policy_targets WHERE id=$1::uuid", str(identifier),
    ))
    if not rows:
        raise _Abort("not_found")
    return rows[0]


async def _validate_target(writer: NativeAuditTransaction, target: _Target, catalog: tuple[str, ...]) -> None:
    if target.kind == "model":
        if target.native_id not in catalog:
            raise _Abort("forbidden")
        return
    current: Final = await _native(writer, target.kind, target.native_id)
    if current is None or _anchor(current) != target.anchor:
        raise _Abort("forbidden")


async def _resources(writer: NativeAuditTransaction, kind: ResourceKind, identifiers: tuple[UUID, ...]) -> tuple[Resource, ...]:
    if kind == "bindings":
        query: Final = '''SELECT b.id,b.target_id,b.policy_id,b.enabled,b.version,b.created_at,b.updated_at,
        t.kind AS target_kind,t.label AS target_label,p.name AS policy_name FROM retail_request_policy_bindings b
        JOIN retail_request_policy_targets t ON t.id=b.target_id JOIN retail_request_policy_policies p ON p.id=b.policy_id
        WHERE b.id=ANY($1::uuid[]) AND NOT b.deleted ORDER BY b.created_at,b.id'''
        adapter: Final = TypeAdapter(tuple[BindingRow, ...])
    elif kind == "policies":
        query: Final = "SELECT id,name,definition,prompt_id,enabled,version,created_at,updated_at FROM retail_request_policy_policies WHERE id=ANY($1::uuid[]) AND NOT deleted ORDER BY created_at,id"
        adapter: Final = TypeAdapter(tuple[PolicyRow, ...])
    else:
        query: Final = "SELECT id,name,content,enabled,version,created_at,updated_at FROM retail_request_policy_prompts WHERE id=ANY($1::uuid[]) AND NOT deleted ORDER BY created_at,id"
        adapter: Final = TypeAdapter(tuple[PromptRow, ...])
    return adapter.validate_python(await writer.query_raw(query, [str(identifier) for identifier in identifiers]))


async def _resource(writer: NativeAuditTransaction, kind: ResourceKind, identifier: UUID) -> Resource:
    rows: Final = await _resources(writer, kind, (identifier,))
    if not rows:
        raise _Abort("not_found")
    return rows[0]


def _metadata(row: BaseModel) -> Mapping[str, JsonValue]:
    return {key: value for key, value in _json(row).items() if key != "content"}


async def _reference(writer: NativeAuditTransaction, table: str, identifier: UUID | None) -> None:
    if identifier is None:
        return
    count: Final = _COUNTS.validate_python(await writer.query_raw(
        f"SELECT count(*) AS total FROM {table} WHERE id=$1::uuid AND NOT deleted", str(identifier),
    ))[0].total
    if not count:
        raise _Abort("not_found")


async def _binding_target(writer: NativeAuditTransaction, data: BindingCreate, catalog: tuple[str, ...]) -> None:
    target: Final = await _target(writer, data.target_id)
    await _validate_target(writer, target, catalog)
    count: Final = _COUNTS.validate_python(await writer.query_raw(
        "SELECT count(*) AS total FROM retail_request_policy_bindings WHERE target_id=$1::uuid AND NOT deleted", str(data.target_id),
    ))[0].total
    if count:
        raise _Abort("conflict")


async def _mutate(
    writer: NativeAuditTransaction, kind: ResourceKind, identifier: UUID, data: WriteInput | None,
    *, creating: bool, expected: int | None, catalog: tuple[str, ...],
) -> Mapping[str, JsonValue]:
    if not creating:
        current: Final = await _resource(writer, kind, identifier)
        if current.version != expected:
            raise _Abort("conflict")
    if data is None:
        table: Final = _TABLES[kind]
        if kind == "policies":
            refs: Final = await writer.query_raw("SELECT count(*) AS total FROM retail_request_policy_bindings WHERE policy_id=$1::uuid AND enabled AND NOT deleted", str(identifier))
        elif kind == "prompts":
            refs: Final = await writer.query_raw("SELECT count(*) AS total FROM retail_request_policy_policies WHERE prompt_id=$1::uuid AND NOT deleted", str(identifier))
        else:
            refs: Final = [{"total": 0}]
        if _COUNTS.validate_python(refs)[0].total:
            raise _Abort("conflict")
        await writer.query_raw(f"UPDATE {table} SET deleted=true,enabled=false,version=version+1,updated_at=clock_timestamp() WHERE id=$1::uuid RETURNING id", str(identifier))
        return {"id": str(identifier), "deleted": True, "version": (expected or 0)+1}
    if isinstance(data, (PromptCreate, PromptUpdate)):
        if creating:
            await _capacity(writer, "prompt")
            await writer.query_raw("INSERT INTO retail_request_policy_prompts(id,name,content,enabled) VALUES($1::uuid,$2,$3,$4) RETURNING id", str(identifier), data.name, data.content, data.enabled)
        else:
            await writer.query_raw("UPDATE retail_request_policy_prompts SET name=$2,content=$3,enabled=$4,version=version+1,updated_at=clock_timestamp() WHERE id=$1::uuid RETURNING id", str(identifier), data.name, data.content, data.enabled)
    elif isinstance(data, (PolicyCreate, PolicyUpdate)):
        await _reference(writer, "retail_request_policy_prompts", data.prompt_id)
        if creating:
            await _capacity(writer, "policy")
            await writer.query_raw("INSERT INTO retail_request_policy_policies(id,name,definition,prompt_id,enabled) VALUES($1::uuid,$2,$3::jsonb,$4::uuid,$5) RETURNING id", str(identifier), data.name, data.definition.model_dump_json(), str(data.prompt_id) if data.prompt_id else None, data.enabled)
        else:
            await writer.query_raw("UPDATE retail_request_policy_policies SET name=$2,definition=$3::jsonb,prompt_id=$4::uuid,enabled=$5,version=version+1,updated_at=clock_timestamp() WHERE id=$1::uuid RETURNING id", str(identifier), data.name, data.definition.model_dump_json(), str(data.prompt_id) if data.prompt_id else None, data.enabled)
    else:
        if isinstance(data, BindingCreate) or data.enabled:
            await _reference(writer, "retail_request_policy_policies", data.policy_id)
        if isinstance(data, BindingCreate):
            await _binding_target(writer, data, catalog)
            await _capacity(writer, "binding")
            await writer.query_raw("INSERT INTO retail_request_policy_bindings(id,target_id,policy_id,enabled) VALUES($1::uuid,$2::uuid,$3::uuid,$4) RETURNING id", str(identifier), str(data.target_id), str(data.policy_id), data.enabled)
        else:
            binding: Final = BindingRow.model_validate(_json(await _resource(writer, "bindings", identifier)))
            if data.enabled:
                await _validate_target(writer, await _target(writer, binding.target_id), catalog)
            await writer.query_raw("UPDATE retail_request_policy_bindings SET policy_id=$2::uuid,enabled=$3,version=version+1,updated_at=clock_timestamp() WHERE id=$1::uuid RETURNING id", str(identifier), str(data.policy_id), data.enabled)
    return _metadata(await _resource(writer, kind, identifier))


async def _target_item(writer: NativeAuditTransaction, kind: TargetKind, native: _NativeTarget) -> JsonValue:
    anchor: Final = _digest({"model": native.native_id}) if kind == "model" else _anchor(native)
    existing: Final = _TARGETS.validate_python(await writer.query_raw("SELECT * FROM retail_request_policy_targets WHERE kind=$1 AND native_id=$2 AND anchor=$3", kind, native.native_id, anchor))
    identifier: Final = existing[0].id if existing else uuid4()
    if not existing:
        await _capacity(writer, "target")
        await writer.query_raw("INSERT INTO retail_request_policy_targets(id,kind,native_id,anchor,native_created_at,owner_id,owner_created_at,label) VALUES($1::uuid,$2,$3,$4,$5::timestamptz,$6,$7::timestamptz,$8) RETURNING id", str(identifier), kind, native.native_id, anchor, native.native_created_at, native.owner_id, native.owner_created_at, safe_display_name(native.label, f"{kind.title()} {identifier.hex[:8]}"))
    target: Final = await _target(writer, identifier)
    return _json(PolicyTarget(id=target.id, kind=target.kind, label=target.label))


class NativeRequestPolicyStore:
    def __init__(self, native_client: RequestPolicyClient, *, catalog: Callable[[], tuple[str, ...]] | None = None) -> None:
        self._client = native_client
        self._catalog = catalog
        self._closed = False

    @asynccontextmanager
    async def _transaction(self, *, read: bool = False) -> AsyncIterator[NativeAuditTransaction]:
        if self._closed:
            raise _Abort("unavailable")
        timeout: Final = 2 if read else 8
        async with asyncio.timeout(timeout), _SLOTS, self._client.tx(timeout=timedelta(seconds=timeout)) as writer:
            if read:
                await writer.execute_raw("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            await writer.execute_raw("SET LOCAL lock_timeout='500ms'")
            await writer.execute_raw("SET LOCAL statement_timeout='1500ms'")
            yield writer

    def _models(self) -> tuple[str, ...]:
        if self._catalog is None:
            raise _Abort("unavailable")
        return tuple(sorted(frozenset(name for name in self._catalog() if 0<len(name)<=200 and not any(ord(c)<32 for c in name))))[:10000]

    async def initialize(self) -> RequestPolicyFailure | None:
        try:
            async with self._transaction() as writer:
                await writer.execute_raw(Path(__file__).with_name("request_policy_schema.sql").read_text(encoding="utf-8"))
                await writer.query_raw("INSERT INTO retail_request_policy_settings(singleton) VALUES(true) ON CONFLICT DO NOTHING RETURNING singleton")
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError):
            return RequestPolicyFailure("unavailable")
        return None

    async def list_resources(self, actor: PolicyActor, kind: ResourceKind, *, limit: int = 25, offset: int = 0) -> Reply:
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
            return RequestPolicyFailure("invalid")
        try:
            async with self._transaction(read=True) as writer:
                await _admin(writer, actor)
                count: Final = _COUNTS.validate_python(await writer.query_raw(f"SELECT count(*) AS total FROM {_TABLES[kind]} WHERE NOT deleted"))[0].total
                ids: Final = await writer.query_raw(f"SELECT id::text AS id FROM {_TABLES[kind]} WHERE NOT deleted ORDER BY created_at,id LIMIT $1 OFFSET $2", limit, offset)
                keys: Final = TypeAdapter(tuple[dict[str, UUID], ...]).validate_python(ids)
                resources: Final = await _resources(writer, kind, tuple(row["id"] for row in keys))
                rows: Final = tuple(_metadata(row) for row in resources)
                return {"items": list(rows), "total": count, "limit": limit, "offset": offset}
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError):
            return RequestPolicyFailure("unavailable")

    async def get_resource(self, actor: PolicyActor, kind: ResourceKind, identifier: UUID) -> Reply:
        try:
            async with self._transaction(read=True) as writer:
                await _admin(writer, actor)
                return _json(await _resource(writer, kind, identifier))
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError):
            return RequestPolicyFailure("unavailable")

    async def write(
        self, actor: PolicyActor, kind: ResourceKind, operation: Literal["created", "updated", "deleted"],
        key: str, data: WriteInput | None, identifier: UUID | None = None, expected_version: int | None = None,
    ) -> Reply:
        try:
            async with self._transaction() as writer:
                actor_id: Final = await _admin(writer, actor)
                await _write_lock(writer)
                digest: Final = _digest({"kind": kind, "operation": operation, "id": str(identifier) if identifier else None,
                    "expected_version": expected_version, "data": _json(data) if data else None})
                operation_key: Final = hashlib.sha256(key.encode()).hexdigest()
                receipts: Final = _RECEIPTS.validate_python(await writer.query_raw(
                    "SELECT digest,response FROM retail_request_policy_receipts WHERE actor_key=$1 AND operation_key=$2", actor_id, operation_key,
                ))
                if receipts:
                    if receipts[0].digest != digest:
                        raise _Abort("conflict")
                    return receipts[0].response
                await _capacity(writer, "receipt")
                result: Final = await _mutate(writer, kind, identifier or uuid4(), data,
                    creating=operation == "created", expected=expected_version,
                    catalog=self._models() if kind == "bindings" and data is not None and (isinstance(data, BindingCreate) or data.enabled) else ())
                await writer.query_raw("INSERT INTO retail_request_policy_receipts(actor_key,operation_key,digest,response) VALUES($1,$2,$3,$4::jsonb) RETURNING actor_key", actor_id, operation_key, digest, json.dumps(result))
                action: Final = f"native_request_policy.{kind[:-1] if kind != 'policies' else 'policy'}_{operation}"
                await append_native_audit(writer, actor_id=actor_id, action=action)
                return result
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError, NativeAuditUnavailable, NativeAuditConflict):
            return RequestPolicyFailure("unavailable")

    async def targets(self, actor: PolicyActor, kind: TargetKind, *, q: str = "", limit: int = 25, offset: int = 0) -> Reply:
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000 or len(q)>100:
            return RequestPolicyFailure("invalid")
        try:
            async with self._transaction() as writer:
                await _admin(writer, actor)
                await _write_lock(writer)
                if kind == "model":
                    names: Final = tuple(name for name in self._models() if q.casefold() in name.casefold())
                    natives: Final = tuple(_NativeTarget(native_id=name, native_created_at=None, owner_id=None, owner_created_at=None, label=name) for name in names[offset:offset+limit])
                    total: Final = len(names)
                else:
                    if kind == "user":
                        projection: Final = "user_id AS native_id,created_at AT TIME ZONE 'UTC' AS native_created_at,NULL::text AS owner_id,NULL::timestamptz AS owner_created_at,user_alias AS label"
                        source: Final = '"LiteLLM_UserTable"'
                        condition: Final = "created_at IS NOT NULL AND (position(lower($1) in lower(user_id))>0 OR position(lower($1) in lower(coalesce(user_alias,'')))>0)"
                    else:
                        projection: Final = "k.token AS native_id,k.created_at AT TIME ZONE 'UTC' AS native_created_at,k.user_id AS owner_id,u.created_at AT TIME ZONE 'UTC' AS owner_created_at,k.key_alias AS label"
                        source: Final = '"LiteLLM_VerificationToken" k LEFT JOIN "LiteLLM_UserTable" u ON u.user_id=k.user_id'
                        condition: Final = "k.created_at IS NOT NULL AND (k.user_id IS NULL OR u.created_at IS NOT NULL) AND position(lower($1) in lower(coalesce(k.key_alias,'')))>0"
                    total: Final = _COUNTS.validate_python(await writer.query_raw(f"SELECT count(*) AS total FROM {source} WHERE {condition}", q))[0].total
                    natives: Final = _NATIVES.validate_python(await writer.query_raw(f"SELECT {projection} FROM {source} WHERE {condition} ORDER BY native_id LIMIT $2 OFFSET $3", q, limit, offset))
                items: Final = tuple([await _target_item(writer, kind, native) for native in natives])
                return {"items": list(items), "total": total, "limit": limit, "offset": offset}
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError):
            return RequestPolicyFailure("unavailable")

    async def lookup(
        self, user_id: str | None, token_hash: str, model: str, *, trusted_master: bool = False,
    ) -> ResolvedRequestPolicies | RequestPolicyFailure:
        try:
            if self._closed:
                raise _Abort("unavailable")
            async with asyncio.timeout(2), _SLOTS:
                rows: Final = _LOOKUPS.validate_python(await self._client.writer_db.query_raw(_LOOKUP_SQL, token_hash, user_id, model, trusted_master))
                if not rows or not rows[0].active or any(not rule.valid for rule in rows[0].rules):
                    raise _Abort("forbidden")
                row: Final = rows[0]
                user: Final = LiteLLM_UserTable.model_validate(row.user_data) if row.user_data else None
                if trusted_master:
                    auth: Final = UserAPIKeyAuth(user_id=user_id, user_role=LitellmUserRoles.PROXY_ADMIN).model_copy(update={"api_key": LITELLM_PROXY_MASTER_KEY_ALIAS, "token": token_hash,"via_virtual_key": True})
                else:
                    if row.key_data is None:
                        raise _Abort("forbidden")
                    auth: Final = UserAPIKeyAuth.model_validate(row.key_data).model_copy(update={
                        "api_key": token_hash, "token": token_hash,
                        "user_role": LitellmUserRoles(user.user_role) if user and user.user_role else None,
                    })
                policies: Final = tuple(ApplicablePolicy(rule.target_kind, rule.policy_id, rule.policy_version,
                    rule.definition, rule.prompt_text, rule.prompt_id, rule.prompt_version) for rule in row.rules)
                return ResolvedRequestPolicies(auth, user, token_hash, policies)
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError, ValueError):
            return RequestPolicyFailure("unavailable")

    async def close(self) -> None:
        self._closed = True


_LOOKUP_SQL: Final = '''WITH identity AS (
    SELECT to_jsonb(k) AS key_data,to_jsonb(u)-'password'-'last_breach_check_at' AS user_data,
    (k.blocked IS DISTINCT FROM true AND (k.expires IS NULL OR (k.expires AT TIME ZONE 'UTC')>clock_timestamp())
        AND (k.user_id IS NULL OR (u.user_id IS NOT NULL AND u.password_reset_required IS DISTINCT FROM true
            AND u.metadata->'scim_active' IS DISTINCT FROM 'false'::jsonb))) AS active,
    k.created_at AT TIME ZONE 'UTC' AS key_created_at,u.created_at AT TIME ZONE 'UTC' AS user_created_at
    FROM "LiteLLM_VerificationToken" k LEFT JOIN "LiteLLM_UserTable" u ON u.user_id=k.user_id
    WHERE NOT $4 AND k.token=$1 AND k.user_id IS NOT DISTINCT FROM $2
    UNION ALL
    SELECT NULL::jsonb,to_jsonb(u)-'password'-'last_breach_check_at',
        (u.user_id IS NULL OR (u.user_role='proxy_admin' AND u.password_reset_required IS DISTINCT FROM true
            AND u.metadata->'scim_active' IS DISTINCT FROM 'false'::jsonb)),
        NULL::timestamptz,u.created_at AT TIME ZONE 'UTC'
    FROM (SELECT 1) seed LEFT JOIN "LiteLLM_UserTable" u ON u.user_id=$2 WHERE $4
), rules AS (
    SELECT t.kind AS target_kind,p.id AS policy_id,p.version AS policy_version,p.definition,p.prompt_id,
        s.version AS prompt_version,s.content AS prompt_text,
        coalesce((p.id IS NOT NULL AND p.enabled AND NOT p.deleted AND (p.prompt_id IS NULL OR (s.enabled AND NOT s.deleted))
        AND (t.kind='model' OR (t.kind='user' AND t.native_created_at=i.user_created_at)
            OR (t.kind='key' AND t.native_created_at=i.key_created_at AND t.owner_id IS NOT DISTINCT FROM $2
                AND t.owner_created_at IS NOT DISTINCT FROM i.user_created_at))),false) AS valid
    FROM identity i JOIN retail_request_policy_targets t
        ON (t.kind='model' AND t.native_id=$3) OR (t.kind='user' AND t.native_id=$2 AND i.user_data IS NOT NULL)
        OR (t.kind='key' AND t.native_id=$1 AND NOT $4)
    JOIN retail_request_policy_bindings b ON b.target_id=t.id AND b.enabled AND NOT b.deleted
    LEFT JOIN retail_request_policy_policies p ON p.id=b.policy_id
    LEFT JOIN retail_request_policy_prompts s ON s.id=p.prompt_id
)
SELECT i.key_data,i.user_data,i.active,coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY
    CASE r.target_kind WHEN 'model' THEN 0 WHEN 'user' THEN 1 ELSE 2 END) FROM rules r),'[]'::jsonb) AS rules
FROM identity i'''
