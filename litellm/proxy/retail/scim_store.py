import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

from prisma.errors import PrismaError
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from .control_plane_flags import native_audit_enabled
from .native_audit_store import (
    NativeAuditClient,
    NativeAuditConflict,
    NativeAuditTransaction,
    NativeAuditUnavailable,
    append_native_audit,
)
from .scim_config import ScimSetup
from .scim_protocol import (
    SCIM_USER_PREFIX,
    ScimFailure,
    ScimList,
    ScimProfile,
    ScimQuery,
    ScimRecord,
    apply_patch,
    matches_etag,
    parse_replace,
)

_SLOTS: Final = asyncio.Semaphore(4)
_COLUMNS: Final = (
    "id,user_id,namespace,external_id,user_name,display_name,active,version,credential_revision,created_at,updated_at"
)
_RECORDS: Final = TypeAdapter(tuple[ScimRecord, ...])


class _Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    namespace: str
    generation: int
    digest: str
    max_users: int
    retained_count: int


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    total: int


class _Owner(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_role: str | None


_SETTINGS: Final = TypeAdapter(tuple[_Settings, ...])
_COUNTS: Final = TypeAdapter(tuple[_Count, ...])
_OWNERS: Final = TypeAdapter(tuple[_Owner, ...])


class _Abort(Exception):
    def __init__(self, failure: ScimFailure) -> None:
        self.failure = failure


async def _settings(writer: NativeAuditTransaction, *, lock: bool = False) -> _Settings | None:
    rows: Final = _SETTINGS.validate_python(await writer.query_raw(
        "SELECT namespace,generation,digest,max_users,retained_count FROM retail_scim_settings WHERE singleton"
        + (" FOR UPDATE" if lock else ""),
    ))
    return rows[0] if rows else None


async def _authority(writer: NativeAuditTransaction, setup: ScimSetup) -> _Settings:
    current: Final = await _settings(writer)
    if current is None or (current.namespace, current.generation, current.digest) != (
        setup.config.namespace, setup.config.generation, setup.digest(),
    ):
        raise _Abort(ScimFailure(503))
    return current


async def _write_lock(writer: NativeAuditTransaction) -> None:
    if not native_audit_enabled():
        raise _Abort(ScimFailure(503))
    await writer.query_raw("SELECT pg_advisory_xact_lock(799405132)::text")


async def _record(
    writer: NativeAuditTransaction, setup: ScimSetup, identifier: UUID, *, lock: bool = False,
) -> ScimRecord:
    rows: Final = _RECORDS.validate_python(await writer.query_raw(
        f"SELECT {_COLUMNS} FROM retail_scim_users WHERE namespace=$1 AND id=$2::uuid AND NOT deleted"
        + (" FOR UPDATE" if lock else ""), setup.config.namespace, str(identifier),
    ))
    if not rows:
        raise _Abort(ScimFailure(404))
    return rows[0]


async def _customer(writer: NativeAuditTransaction, record: ScimRecord) -> None:
    rows: Final = _OWNERS.validate_python(await writer.query_raw(
        'SELECT user_role FROM "LiteLLM_UserTable" WHERE user_id=$1', record.user_id,
    ))
    if not rows or rows[0].user_role != "customer":
        raise _Abort(ScimFailure(403))


async def _unique(
    writer: NativeAuditTransaction, setup: ScimSetup, profile: ScimProfile, identifier: UUID,
) -> None:
    rows: Final = _COUNTS.validate_python(await writer.query_raw(
        """SELECT count(*) AS total FROM retail_scim_users WHERE namespace=$1 AND NOT deleted AND id<>$4::uuid
        AND (external_id=$2 OR user_name_normalized=$3)""",
        setup.config.namespace, profile.external_id, profile.user_name.lower(), str(identifier),
    ))
    if rows[0].total:
        raise _Abort(ScimFailure(409, "uniqueness"))


def _where(query: ScimQuery) -> tuple[str, tuple[object, ...]]:
    match query.filter_field:
        case None:
            return "", ()
        case "id":
            return " AND id::text=$2", (query.filter_value,)
        case "external_id":
            return " AND external_id=$2", (query.filter_value,)
        case "user_name":
            return " AND user_name_normalized=$2", (query.filter_value,)
        case "active":
            return " AND active=$2", (query.filter_value,)


async def _configure(writer: NativeAuditTransaction, setup: ScimSetup) -> None:
    await _write_lock(writer)
    current: Final = await _settings(writer, lock=True)
    if current is not None:
        if (current.namespace, current.generation, current.digest) == (
            setup.config.namespace, setup.config.generation, setup.digest(),
        ):
            return
        if (current.namespace != setup.config.namespace or current.generation >= setup.config.generation
                or current.retained_count > setup.config.max_users):
            raise _Abort(ScimFailure(409))
    await writer.query_raw(
        """INSERT INTO retail_scim_settings(singleton,namespace,generation,digest,max_users)
        VALUES(true,$1,$2,$3,$4) ON CONFLICT(singleton) DO UPDATE SET generation=EXCLUDED.generation,
        digest=EXCLUDED.digest,max_users=EXCLUDED.max_users RETURNING singleton""",
        setup.config.namespace, setup.config.generation, setup.digest(), setup.config.max_users,
    )
    await append_native_audit(writer, actor_id="system:scim-directory", action="native_scim.configured")


async def _create(writer: NativeAuditTransaction, setup: ScimSetup, profile: ScimProfile) -> ScimRecord:
    await _write_lock(writer)
    current: Final = await _authority(writer, setup)
    identifier: Final = uuid4()
    await _unique(writer, setup, profile, identifier)
    if current.retained_count >= current.max_users:
        raise _Abort(ScimFailure(503))
    user_id: Final = SCIM_USER_PREFIX + str(uuid4())
    await writer.query_raw("SELECT set_config('retail.scim_provision','on',true)")
    await writer.query_raw(
        '''INSERT INTO "LiteLLM_UserTable"(user_id,user_role,models)
        VALUES($1,'customer',ARRAY[]::text[]) RETURNING user_id''', user_id,
    )
    await writer.query_raw("SELECT set_config('retail.scim_provision','off',true)")
    rows: Final = _RECORDS.validate_python(await writer.query_raw(
        f"""INSERT INTO retail_scim_users(id,user_id,namespace,external_id,user_name,user_name_normalized,
        display_name,active,credential_revision) VALUES($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9::uuid)
        RETURNING {_COLUMNS}""", str(identifier), user_id, setup.config.namespace, profile.external_id,
        profile.user_name, profile.user_name.lower(), profile.display_name, profile.active, str(uuid4()),
    ))
    await writer.query_raw(
        "UPDATE retail_scim_settings SET retained_count=retained_count+1 WHERE singleton RETURNING singleton",
    )
    await append_native_audit(writer, actor_id="system:scim-directory", action="native_scim.user_created")
    return rows[0]


async def _update(
    writer: NativeAuditTransaction, setup: ScimSetup, identifier: UUID, body: bytes, *,
    replace: bool, if_match: str | None,
) -> ScimRecord:
    await _write_lock(writer)
    await _authority(writer, setup)
    current: Final = await _record(writer, setup, identifier, lock=True)
    await _customer(writer, current)
    matched: Final = matches_etag(if_match, current)
    if isinstance(matched, ScimFailure):
        raise _Abort(matched)
    if not matched:
        raise _Abort(ScimFailure(412))
    change: Final = parse_replace(body, current) if replace else apply_patch(body, current)
    if isinstance(change, ScimFailure):
        raise _Abort(change)
    original: Final = ScimProfile(external_id=current.external_id, user_name=current.user_name,
        display_name=current.display_name, active=current.active)
    if change.profile == original and not change.rotate_credentials:
        return current
    await _unique(writer, setup, change.profile, identifier)
    rows: Final = _RECORDS.validate_python(await writer.query_raw(
        f"""UPDATE retail_scim_users SET user_name=$3,user_name_normalized=$4,display_name=$5,active=$6,
        version=version+1,credential_revision=$7::uuid,updated_at=clock_timestamp()
        WHERE namespace=$1 AND id=$2::uuid RETURNING {_COLUMNS}""", setup.config.namespace, str(identifier),
        change.profile.user_name, change.profile.user_name.lower(), change.profile.display_name, change.profile.active,
        str(uuid4() if change.rotate_credentials else current.credential_revision),
    ))
    await append_native_audit(writer, actor_id="system:scim-directory", action="native_scim.user_updated")
    return rows[0]


async def _delete(
    writer: NativeAuditTransaction, setup: ScimSetup, identifier: UUID, if_match: str | None,
) -> None:
    await _write_lock(writer)
    await _authority(writer, setup)
    current: Final = await _record(writer, setup, identifier, lock=True)
    await _customer(writer, current)
    matched: Final = matches_etag(if_match, current)
    if isinstance(matched, ScimFailure):
        raise _Abort(matched)
    if not matched:
        raise _Abort(ScimFailure(412))
    await writer.query_raw(
        """UPDATE retail_scim_users SET active=false,deleted=true,version=version+1,
        credential_revision=$3::uuid,updated_at=clock_timestamp() WHERE namespace=$1 AND id=$2::uuid RETURNING id""",
        setup.config.namespace, str(identifier), str(uuid4()),
    )
    await append_native_audit(writer, actor_id="system:scim-directory", action="native_scim.user_deleted")


class NativeScimStore:
    def __init__(self, native_client: NativeAuditClient) -> None:
        self._client = native_client
        self._closed = False

    @asynccontextmanager
    async def _transaction(self, timeout: float, *, read: bool = False) -> AsyncIterator[NativeAuditTransaction]:
        if self._closed:
            raise _Abort(ScimFailure(503))
        async with asyncio.timeout(timeout), _SLOTS, self._client.tx(timeout=timedelta(seconds=timeout)) as writer:
            if read:
                await writer.execute_raw("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            await writer.execute_raw("SET LOCAL lock_timeout='500ms'")
            await writer.execute_raw("SET LOCAL statement_timeout='1500ms'")
            yield writer

    async def initialize(self) -> ScimFailure | None:
        try:
            async with self._transaction(8) as writer:
                await writer.execute_raw(Path(__file__).with_name("scim_schema.sql").read_text(encoding="utf-8"))
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError):
            return ScimFailure(503)
        return None

    async def configure(self, setup: ScimSetup) -> ScimFailure | None:
        try:
            async with self._transaction(8) as writer:
                await _configure(writer, setup)
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError, NativeAuditUnavailable, NativeAuditConflict):
            return ScimFailure(503)
        return None

    async def list_users(self, setup: ScimSetup, query: ScimQuery) -> ScimList | ScimFailure:
        try:
            async with self._transaction(2, read=True) as writer:
                await _authority(writer, setup)
                clause, values = _where(query)
                parameters: Final = (setup.config.namespace, *values)
                counts: Final = _COUNTS.validate_python(await writer.query_raw(
                    "SELECT count(*) AS total FROM retail_scim_users WHERE namespace=$1 AND NOT deleted" + clause,
                    *parameters,
                ))
                records: Final = _RECORDS.validate_python(await writer.query_raw(
                    f"SELECT {_COLUMNS} FROM retail_scim_users WHERE namespace=$1 AND NOT deleted" + clause
                    + f" ORDER BY created_at,id LIMIT ${len(parameters)+1} OFFSET ${len(parameters)+2}",
                    *parameters, query.count, min(query.start_index-1, 10000),
                ))
                return ScimList(resources=records, total=counts[0].total, start_index=query.start_index)
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError):
            return ScimFailure(503)

    async def get_user(self, setup: ScimSetup, identifier: UUID) -> ScimRecord | ScimFailure:
        try:
            async with self._transaction(2, read=True) as writer:
                await _authority(writer, setup)
                return await _record(writer, setup, identifier)
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError):
            return ScimFailure(503)

    async def create_user(self, setup: ScimSetup, profile: ScimProfile) -> ScimRecord | ScimFailure:
        try:
            async with self._transaction(8) as writer:
                return await _create(writer, setup, profile)
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError, NativeAuditUnavailable, NativeAuditConflict):
            return ScimFailure(503)

    async def update_user(
        self, setup: ScimSetup, identifier: UUID, body: bytes, *, replace: bool, if_match: str | None,
    ) -> ScimRecord | ScimFailure:
        try:
            async with self._transaction(8) as writer:
                return await _update(writer, setup, identifier, body, replace=replace, if_match=if_match)
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError, NativeAuditUnavailable, NativeAuditConflict):
            return ScimFailure(503)

    async def delete_user(self, setup: ScimSetup, identifier: UUID, if_match: str | None) -> ScimFailure | None:
        try:
            async with self._transaction(8) as writer:
                await _delete(writer, setup, identifier, if_match)
        except _Abort as exc:
            return exc.failure
        except (PrismaError, TimeoutError, ValidationError, NativeAuditUnavailable, NativeAuditConflict):
            return ScimFailure(503)
        return None

    async def close(self) -> None:
        self._closed = True
