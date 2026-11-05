import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta, timezone
from pathlib import Path
from typing import Final, Literal, Protocol, TypeAlias
from uuid import UUID, uuid4

from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, field_validator

from .control_plane_flags import native_audit_enabled
from .operation_events import append_operation_event
from .store import _one

NativeAuditAction: TypeAlias = Literal[
    "native_key.rotated", "native_password.changed", "native_session.revoked",
    "native_identity.configured", "native_identity.signed_in",
    "native_api_identity.configured",
]
NativeAuditFailure: TypeAlias = Literal["bridge_unavailable", "integrity_conflict"]


class NativeAuditWriter(Protocol):
    async def query_raw(self, query: str, *args: object) -> object: ...


class NativeAuditTransaction(NativeAuditWriter, Protocol):
    async def execute_raw(self, query: str, *args: object) -> int: ...


class NativeAuditClient(Protocol):
    def tx(self, *, timeout: timedelta) -> AbstractAsyncContextManager[NativeAuditTransaction]: ...


class NativeAuditUnavailable(Exception):
    pass


class NativeAuditConflict(Exception):
    pass


class NativeAuditMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    actor_id: str = Field(min_length=1, max_length=191)
    action: NativeAuditAction
    affected_count: StrictInt = Field(ge=0, le=9_223_372_036_854_775_807)

    @field_validator("actor_id")
    @classmethod
    def clean_actor(cls, value: str) -> str:
        if value != value.strip() or any(ord(char)<32 for char in value):
            raise ValueError("A server-verified native account identifier is required")
        return value


class NativeAuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    source_id: UUID
    actor_id: str
    action: NativeAuditAction
    resource_id: UUID
    affected_count: int
    occurred_at: AwareDatetime

    def operation_key(self) -> str:
        return f"native-audit:{self.source_id.hex}:{self.id.hex}"

    def digest(self) -> str:
        payload: Final = {
            "id":str(self.id),"source_id":str(self.source_id),"actor_id":self.actor_id,"action":self.action,
            "resource_id":str(self.resource_id),"affected_count":self.affected_count,
            "occurred_at":self.occurred_at.astimezone(timezone.utc).isoformat(),
        }
        return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()


class NativeAuditClaim(NativeAuditEvent):
    lease_owner: str
    lease_token: UUID
    attempts: int


class NativeAuditCounts(BaseModel):
    model_config = ConfigDict(frozen=True)
    pending: int
    processing: int
    failed: int
    retained: int


class _Settings(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_id: UUID
    retained_count: int


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True)
    total: int


class _Acquired(BaseModel):
    model_config = ConfigDict(frozen=True)
    acquired: bool


class _ExistingEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: str
    resource_type: str
    resource_id: str
    request_digest: str


class _Identifier(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID


_SETTINGS: Final = TypeAdapter(tuple[_Settings,...])
_EVENTS: Final = TypeAdapter(tuple[NativeAuditEvent,...])
_CLAIMS: Final = TypeAdapter(tuple[NativeAuditClaim,...])
_COUNTS: Final = TypeAdapter(tuple[_Count,...])
_ACQUIRED: Final = TypeAdapter(tuple[_Acquired,...])
_IDENTIFIERS: Final = TypeAdapter(tuple[_Identifier,...])
_EVENT_COLUMNS: Final = "id,source_id,actor_id,action,resource_id,affected_count,occurred_at"


async def ensure_native_schema(client: NativeAuditClient) -> None:
    schema: Final = Path(__file__).with_name("native_audit_schema.sql").read_text(encoding="utf-8")
    async with client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw("SET LOCAL lock_timeout='1000ms'")
        await writer.execute_raw("SET LOCAL statement_timeout='7000ms'")
        await writer.execute_raw(schema)


async def append_native_audit(
    writer: NativeAuditWriter, *, actor_id: str, action: NativeAuditAction, affected_count: int = 1,
    event_id: UUID | None = None,
) -> UUID | None:
    if not native_audit_enabled():
        return None
    metadata: Final = NativeAuditMetadata(actor_id=actor_id,action=action,affected_count=affected_count)
    identifier: Final = event_id or uuid4()
    await writer.query_raw("""INSERT INTO retail_native_audit_settings(singleton,source_id)
        VALUES(true,gen_random_uuid()) ON CONFLICT(singleton) DO NOTHING RETURNING source_id""")
    settings: Final = _SETTINGS.validate_python(await writer.query_raw(
        "SELECT source_id,retained_count FROM retail_native_audit_settings WHERE singleton FOR UPDATE",
    ))
    if not settings:
        raise NativeAuditUnavailable("Native audit storage is unavailable")
    existing: Final = _EVENTS.validate_python(await writer.query_raw(
        f"SELECT {_EVENT_COLUMNS} FROM retail_native_audit_outbox WHERE id=$1::uuid",str(identifier),
    ))
    if existing:
        if (existing[0].actor_id,existing[0].action,existing[0].affected_count,existing[0].source_id) != (
            metadata.actor_id,metadata.action,metadata.affected_count,settings[0].source_id,
        ):
            raise NativeAuditConflict("The native audit operation identifier was already used")
        return identifier
    if settings[0].retained_count>=10000:
        raise NativeAuditUnavailable("Native audit queue is full")
    await writer.query_raw("""INSERT INTO retail_native_audit_outbox
        (id,source_id,actor_id,action,resource_id,affected_count)
        VALUES($1::uuid,$2::uuid,$3,$4,$1::uuid,$5) RETURNING id""",
        str(identifier),str(settings[0].source_id),metadata.actor_id,metadata.action,metadata.affected_count)
    await writer.query_raw("""UPDATE retail_native_audit_settings SET retained_count=retained_count+1
        WHERE singleton RETURNING source_id""")
    return identifier


class NativeAuditStore:
    def __init__(self, native_client: NativeAuditClient, retail_database_url: str) -> None:
        self.native = native_client
        self._native_slots = asyncio.Semaphore(2)
        self._open_lock = asyncio.Lock()
        self._initialized = False
        options: Final = conninfo_to_dict(retail_database_url).get("options","")
        self.pool = AsyncConnectionPool(
            retail_database_url,open=False,min_size=0,max_size=2,max_waiting=2,timeout=2,
            name="retail-native-audit",check=AsyncConnectionPool.check_connection,
            kwargs={"row_factory":dict_row,"autocommit":True,"connect_timeout":2,
                    "options":f"{options} -c statement_timeout=3000 -c lock_timeout=500 -c idle_in_transaction_session_timeout=5000"},
        )

    async def initialize(self) -> None:
        async with self._open_lock:
            if self._initialized:
                return
            await ensure_native_schema(self.native)
            await self.pool.open()
            self._initialized = True

    @asynccontextmanager
    async def _native_transaction(self) -> AsyncIterator[NativeAuditTransaction]:
        async with asyncio.timeout(5), self._native_slots, self.native.tx(timeout=timedelta(seconds=4)) as writer:
            await writer.execute_raw("SET LOCAL lock_timeout='500ms'")
            await writer.execute_raw("SET LOCAL statement_timeout='3000ms'")
            yield writer

    async def claim(self, owner: str) -> NativeAuditClaim | None:
        async with self._native_transaction() as writer:
            acquired: Final = _ACQUIRED.validate_python(await writer.query_raw(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('retail:native-audit:claim',0)) AS acquired",
            ))
            if not acquired or not acquired[0].acquired:
                return None
            active: Final = _COUNTS.validate_python(await writer.query_raw("""SELECT count(*) AS total
                FROM retail_native_audit_outbox WHERE state='processing' AND lease_until>clock_timestamp()"""))
            if not active or active[0].total>=2:
                return None
            rows: Final = _CLAIMS.validate_python(await writer.query_raw("""WITH candidate AS (
                SELECT id FROM retail_native_audit_outbox WHERE expires_at>clock_timestamp() AND attempts<5
                  AND next_attempt_at<=clock_timestamp()
                  AND (state='pending' OR (state='processing' AND lease_until<=clock_timestamp()))
                ORDER BY occurred_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE retail_native_audit_outbox n SET state='processing',lease_owner=$1,lease_token=$2::uuid,
                  lease_until=clock_timestamp()+interval '30 seconds',attempts=attempts+1,error_code=NULL
                FROM candidate c WHERE n.id=c.id
                RETURNING n.id,n.source_id,n.actor_id,n.action,n.resource_id,n.affected_count,n.occurred_at,
                  n.lease_owner,n.lease_token,n.attempts""",owner,str(uuid4())))
        return rows[0] if rows else None

    async def bridge(self, claim: NativeAuditClaim) -> bool:
        await self.initialize()
        event: Final = NativeAuditEvent.model_validate(claim.model_dump())
        digest: Final = event.digest()
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(event.operation_key(),))
            existing: Final = await _one(connection,"""SELECT action,resource_type,resource_id,request_digest
                FROM retail_operation_events WHERE actor_id=%s AND idempotency_key=%s""",
                (event.actor_id,event.operation_key()),_ExistingEvent)
            if existing is not None:
                if (existing.action,existing.resource_type,existing.resource_id,existing.request_digest) != (
                    event.action,"native_operation",str(event.resource_id),digest,
                ):
                    raise NativeAuditConflict("The native audit receipt does not match its immutable event")
                return False
            await append_operation_event(
                connection,actor_id=event.actor_id,action=event.action,resource_type="native_operation",
                resource_id=str(event.resource_id),organization_id=None,version_before=None,version_after=None,
                changes={"kind":event.action,"row_count":event.affected_count},
                idempotency_key=event.operation_key(),request_digest=digest,
                response={"native_event_id":str(event.id)},
                occurred_at=event.occurred_at,
            )
            return True

    async def acknowledge(self, claim: NativeAuditClaim) -> bool:
        async with self._native_transaction() as writer:
            await writer.query_raw("SELECT source_id FROM retail_native_audit_settings WHERE singleton FOR UPDATE")
            deleted: Final = _IDENTIFIERS.validate_python(await writer.query_raw("""DELETE FROM retail_native_audit_outbox
                WHERE id=$1::uuid AND state='processing' AND lease_owner=$2 AND lease_token=$3::uuid
                  AND lease_until>clock_timestamp() RETURNING id""",str(claim.id),claim.lease_owner,str(claim.lease_token)))
            if not deleted:
                return False
            await writer.query_raw("""UPDATE retail_native_audit_settings SET retained_count=retained_count-1
                WHERE singleton RETURNING source_id""")
            return True

    async def fail(self, claim: NativeAuditClaim, code: NativeAuditFailure = "bridge_unavailable") -> None:
        async with self._native_transaction() as writer:
            await writer.query_raw("""UPDATE retail_native_audit_outbox SET
                state=CASE WHEN $4='integrity_conflict' OR attempts>=5 OR expires_at<=clock_timestamp()
                  THEN 'failed' ELSE 'pending' END,
                next_attempt_at=clock_timestamp()+(LEAST(60,power(2,attempts)) * interval '1 second'),
                error_code=CASE WHEN $4='integrity_conflict' THEN $4 WHEN expires_at<=clock_timestamp() THEN 'retention_expired'
                  WHEN attempts>=5 THEN 'attempts_exhausted' ELSE $4 END,
                lease_owner=NULL,lease_token=NULL,lease_until=NULL
                WHERE id=$1::uuid AND state='processing' AND lease_owner=$2 AND lease_token=$3::uuid RETURNING id""",
                str(claim.id),claim.lease_owner,str(claim.lease_token),code)

    async def maintain(self) -> None:
        async with self._native_transaction() as writer:
            await writer.query_raw("""WITH expired AS (SELECT id FROM retail_native_audit_outbox
                WHERE state<>'failed' AND (expires_at<=clock_timestamp() OR
                  (attempts>=5 AND (state='pending' OR lease_until<=clock_timestamp())))
                ORDER BY occurred_at,id FOR UPDATE SKIP LOCKED LIMIT 20)
                UPDATE retail_native_audit_outbox n SET state='failed',lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                  error_code=CASE WHEN expires_at<=clock_timestamp() THEN 'retention_expired' ELSE 'attempts_exhausted' END
                FROM expired e WHERE n.id=e.id RETURNING n.id""")

    async def requeue_failed(self, event_id: UUID) -> bool:
        async with self._native_transaction() as writer:
            rows: Final = _IDENTIFIERS.validate_python(await writer.query_raw("""UPDATE retail_native_audit_outbox
                SET state='pending',attempts=0,next_attempt_at=clock_timestamp(),expires_at=clock_timestamp()+interval '7 days',
                  error_code=NULL,lease_owner=NULL,lease_token=NULL,lease_until=NULL
                WHERE id=$1::uuid AND state='failed' RETURNING id""",str(event_id)))
        return bool(rows)

    async def counts(self) -> NativeAuditCounts:
        async with self._native_transaction() as writer:
            rows: Final = TypeAdapter(tuple[NativeAuditCounts,...]).validate_python(await writer.query_raw("""SELECT
                count(*) FILTER(WHERE state='pending') AS pending,count(*) FILTER(WHERE state='processing') AS processing,
                count(*) FILTER(WHERE state='failed') AS failed,count(*) AS retained FROM retail_native_audit_outbox"""))
        return rows[0]

    async def release_claims(self, owner: str) -> None:
        async with self._native_transaction() as writer:
            await writer.query_raw("""UPDATE retail_native_audit_outbox SET state='pending',lease_owner=NULL,lease_token=NULL,
                lease_until=NULL WHERE state='processing' AND lease_owner=$1 RETURNING id""",owner)

    async def close(self) -> None:
        await self.pool.close()
