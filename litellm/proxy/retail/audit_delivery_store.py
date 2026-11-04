import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal, TypeAlias
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, JsonValue

from .control_plane_access import ControlPlaneActor
from .operation_events import append_operation_event, operation_digest, replay_operation
from .organization_store import require_admin
from .store import WalletStore, _many, _one, get_store

DeliveryState: TypeAlias = Literal["pending", "processing", "delivered", "failed", "discarded"]
DeliveryErrorCode: TypeAlias = Literal[
    "timeout",
    "transport_error",
    "http_retryable",
    "http_rejected",
    "redirect_rejected",
    "lease_expired",
    "expired",
    "discarded",
]


class AuditDeliveryError(Exception):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class DeliveryEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event_id: UUID
    action: str
    resource_type: str
    result: Literal["succeeded"]
    version_before: int | None
    version_after: int | None
    occurred_at: datetime


class DeliveryClaim(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    event_id: str
    config_fingerprint: str
    lease_token: UUID
    lease_owner: str
    attempts: int
    payload: DeliveryEnvelope
    lease_until: datetime


class DeliveryCounts(BaseModel):
    model_config = ConfigDict(frozen=True)
    pending: int
    processing: int
    delivered: int
    failed: int
    discarded: int


class DeliveryStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool
    config_version: int
    configuration_matches: bool
    activated_at: datetime | None
    retained_count: int
    overflow_count: int
    last_overflow_at: datetime | None
    counts: DeliveryCounts
    last_delivered_at: datetime | None
    last_failure_at: datetime | None
    queue_capacity: Literal[10000] = 10000
    max_attempts: Literal[5] = 5
    retention_days: Literal[7] = 7
    global_concurrency: Literal[2] = 2


class DeliveryJobView(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    event_id: str
    action: str
    resource_type: str
    occurred_at: datetime
    state: DeliveryState
    attempts: int
    version: int
    created_at: datetime
    completed_at: datetime | None
    error_code: DeliveryErrorCode | None
    next_attempt_at: datetime
    configuration_matches: bool


class DeliveryPage(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: tuple[DeliveryJobView, ...]
    total: int
    limit: int
    offset: int


class _Settings(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool
    version: int
    config_fingerprint: str | None
    retained_count: int


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True)
    total: int


class _Version(BaseModel):
    model_config = ConfigDict(frozen=True)
    version: int


_JOB_COLUMNS: Final = """id,event_id,payload->>'action' AS action,payload->>'resource_type' AS resource_type,
    (payload->>'occurred_at')::timestamptz AS occurred_at,state,attempts,version,created_at,completed_at,error_code,
    next_attempt_at,COALESCE(config_fingerprint=%s,false) AS configuration_matches"""
_STATES: Final = frozenset(("pending", "processing", "delivered", "failed", "discarded"))
_ERRORS: Final = frozenset(
    (
        "timeout",
        "transport_error",
        "http_retryable",
        "http_rejected",
        "redirect_rejected",
        "lease_expired",
        "expired",
        "discarded",
    )
)


async def initialize_schema(connection: psycopg.AsyncConnection) -> None:
    await connection.execute(
        Path(__file__).with_name("audit_delivery_schema.sql").read_text(encoding="utf-8"), prepare=False
    )


def _fingerprint(value: str | None) -> None:
    if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AuditDeliveryError("not_configured", 422)


def _version(value: int) -> None:
    if isinstance(value, bool) or not 1 <= value < 9_223_372_036_854_775_807:
        raise AuditDeliveryError("invalid_version", 422)


async def _settings(connection: psycopg.AsyncConnection) -> _Settings:
    row: Final = await _one(
        connection, "SELECT * FROM retail_audit_delivery_settings WHERE singleton FOR UPDATE", (), _Settings
    )
    if row is None:
        raise AuditDeliveryError("storage_unavailable", 503)
    return row


async def _status(connection: psycopg.AsyncConnection, fingerprint: str | None) -> DeliveryStatus:
    row: Final = await _one(
        connection,
        """SELECT s.enabled,s.version AS config_version,COALESCE(s.config_fingerprint=%s,false) AS configuration_matches,
        s.activated_at,s.retained_count,s.overflow_count,s.last_overflow_at,
        (SELECT jsonb_build_object('pending',count(*) FILTER(WHERE state='pending'),
          'processing',count(*) FILTER(WHERE state='processing'),'delivered',count(*) FILTER(WHERE state='delivered'),
          'failed',count(*) FILTER(WHERE state='failed'),'discarded',count(*) FILTER(WHERE state='discarded'))
          FROM retail_audit_deliveries) AS counts,
        (SELECT max(completed_at) FROM retail_audit_deliveries WHERE state='delivered') AS last_delivered_at,
        (SELECT max(completed_at) FROM retail_audit_deliveries WHERE state='failed') AS last_failure_at
        FROM retail_audit_delivery_settings s WHERE singleton""",
        (fingerprint,),
        DeliveryStatus,
    )
    if row is None:
        raise AuditDeliveryError("storage_unavailable", 503)
    return row


async def _job(connection: psycopg.AsyncConnection, identifier: UUID, fingerprint: str | None) -> DeliveryJobView:
    row: Final = await _one(
        connection,
        f"SELECT {_JOB_COLUMNS} FROM retail_audit_deliveries WHERE id=%s",
        (fingerprint, identifier),
        DeliveryJobView,
    )
    if row is None:
        raise AuditDeliveryError("delivery_not_found", 404)
    return row


async def _recover(connection: psycopg.AsyncConnection) -> int:
    changed: Final = await _many(
        connection,
        """WITH candidate AS (SELECT id FROM retail_audit_deliveries
          WHERE (state IN ('pending','processing') AND created_at<=clock_timestamp()-interval '7 days')
            OR (state='processing' AND lease_until<=clock_timestamp())
          ORDER BY created_at,id LIMIT 200 FOR UPDATE SKIP LOCKED)
        UPDATE retail_audit_deliveries j SET
          state=CASE WHEN j.created_at<=clock_timestamp()-interval '7 days' OR j.attempts>=5 THEN 'failed' ELSE 'pending' END,
          completed_at=CASE WHEN j.created_at<=clock_timestamp()-interval '7 days' OR j.attempts>=5
            THEN clock_timestamp() ELSE NULL END,
          error_code=CASE WHEN j.created_at<=clock_timestamp()-interval '7 days' THEN 'expired' ELSE 'lease_expired' END,
          next_attempt_at=clock_timestamp(),lease_owner=NULL,lease_token=NULL,lease_until=NULL,version=j.version+1
        FROM candidate c WHERE j.id=c.id RETURNING j.version""",
        (),
        _Version,
    )
    return len(changed)


class AuditDeliveryStore:
    def __init__(self, wallet: WalletStore) -> None:
        self._wallet = wallet
        self._open_lock = asyncio.Lock()
        self._initialized = False
        options: Final = conninfo_to_dict(wallet.database_url).get("options", "")
        self.pool = AsyncConnectionPool(
            wallet.database_url,
            open=False,
            min_size=0,
            max_size=2,
            max_waiting=4,
            timeout=1,
            name="retail-audit-delivery",
            check=AsyncConnectionPool.check_connection,
            kwargs={
                "row_factory": dict_row,
                "autocommit": True,
                "connect_timeout": 3,
                "options": f"{options} -c statement_timeout=3000 -c lock_timeout=1000 "
                "-c idle_in_transaction_session_timeout=5000",
            },
        )

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._open_lock:
            if self._initialized:
                return
            await self._wallet.initialize()
            await self._wallet.initialize_control_plane()
            await self.pool.open()
            self._initialized = True

    async def close(self) -> None:
        await self.pool.close()

    @asynccontextmanager
    async def _worker_transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        await self.initialize()
        async with self.pool.connection() as connection, connection.transaction():
            yield connection

    @asynccontextmanager
    async def _management_transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        await self._wallet.initialize()
        await self._wallet.initialize_control_plane()
        async with self._wallet.diagnostic_transaction() as connection:
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            await connection.execute("SET LOCAL lock_timeout='1000ms'")
            yield connection

    async def status(self, fingerprint: str | None) -> DeliveryStatus:
        _fingerprint(fingerprint)
        async with self._management_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return await _status(connection, fingerprint)

    async def list(
        self, fingerprint: str | None, *, state: DeliveryState | None = None, limit: int = 25, offset: int = 0
    ) -> DeliveryPage:
        _fingerprint(fingerprint)
        if state not in _STATES and state is not None or not 1 <= limit <= 100 or not 0 <= offset <= 10000:
            raise AuditDeliveryError("invalid_query", 422)
        async with self._management_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            count: Final = await _one(
                connection,
                "SELECT count(*) AS total FROM retail_audit_deliveries WHERE %s::text IS NULL OR state=%s",
                (state, state),
                _Count,
            )
            rows: Final = await _many(
                connection,
                f"SELECT {_JOB_COLUMNS} FROM retail_audit_deliveries WHERE %s::text IS NULL OR state=%s "
                "ORDER BY created_at DESC,id LIMIT %s OFFSET %s",
                (fingerprint, state, state, limit, offset),
                DeliveryJobView,
            )
        return DeliveryPage(items=tuple(rows), total=count.total if count else 0, limit=limit, offset=offset)

    async def _record(
        self,
        connection: psycopg.AsyncConnection,
        actor: ControlPlaneActor,
        key: str,
        action: str,
        digest: str,
        resource_id: str,
        previous: int,
        version: int,
        changes: Mapping[str, JsonValue],
        response: BaseModel,
    ) -> None:
        await append_operation_event(
            connection,
            actor_id=actor.user_id,
            action=action,
            resource_type="audit_delivery",
            resource_id=resource_id,
            organization_id=None,
            version_before=previous,
            version_after=version,
            changes=changes,
            idempotency_key=key,
            request_digest=digest,
            response=response.model_dump(mode="json"),
        )

    async def configure(
        self, enabled: bool, expected_version: int, fingerprint: str | None, actor: ControlPlaneActor, key: str
    ) -> DeliveryStatus:
        require_admin(actor)
        _fingerprint(fingerprint)
        _version(expected_version)
        action: Final = "audit_delivery.updated"
        digest: Final = operation_digest(
            action, {"enabled": enabled, "version": expected_version, "configuration": fingerprint}
        )
        async with self._management_transaction() as connection:
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            if replay is not None:
                return DeliveryStatus.model_validate(replay)
            settings: Final = await _settings(connection)
            if settings.version != expected_version:
                raise AuditDeliveryError("settings_conflict")
            await self._validate_configuration(connection, settings, enabled, fingerprint)
            await connection.execute(
                """UPDATE retail_audit_delivery_settings SET enabled=%s,version=version+1,
                config_fingerprint=CASE WHEN %s THEN %s ELSE config_fingerprint END,
                activated_at=CASE WHEN %s AND NOT enabled THEN clock_timestamp() ELSE activated_at END WHERE singleton""",
                (enabled, enabled, fingerprint, enabled),
            )
            result: Final = await _status(connection, fingerprint)
            await self._record(
                connection,
                actor,
                key,
                action,
                digest,
                "settings",
                settings.version,
                result.config_version,
                {"enabled": enabled},
                result,
            )
            return result

    async def _validate_configuration(
        self, connection: psycopg.AsyncConnection, settings: _Settings, enabled: bool, fingerprint: str | None
    ) -> None:
        if not enabled:
            return
        if fingerprint is None:
            raise AuditDeliveryError("not_configured", 422)
        if settings.config_fingerprint in (None, fingerprint):
            return
        if settings.enabled:
            raise AuditDeliveryError("pause_required")
        outstanding: Final = await _one(
            connection,
            "SELECT count(*) AS total FROM retail_audit_deliveries WHERE state IN ('pending','processing','failed')",
            (),
            _Count,
        )
        if outstanding is not None and outstanding.total:
            raise AuditDeliveryError("outstanding_deliveries")

    async def retry(
        self, job_id: UUID, expected_version: int, fingerprint: str, actor: ControlPlaneActor, key: str
    ) -> DeliveryJobView:
        require_admin(actor)
        _fingerprint(fingerprint)
        _version(expected_version)
        action: Final = "audit_delivery.retried"
        digest: Final = operation_digest(
            action, {"id": str(job_id), "version": expected_version, "configuration": fingerprint}
        )
        async with self._management_transaction() as connection:
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            if replay is not None:
                return DeliveryJobView.model_validate(replay)
            settings: Final = await _settings(connection)
            if not settings.enabled or settings.config_fingerprint != fingerprint:
                raise AuditDeliveryError("configuration_mismatch")
            previous: Final = await _job(connection, job_id, fingerprint)
            if (
                previous.state != "failed"
                or previous.version != expected_version
                or not previous.configuration_matches
                or previous.error_code == "expired"
            ):
                raise AuditDeliveryError("delivery_conflict")
            await connection.execute(
                """UPDATE retail_audit_deliveries SET state='pending',attempts=0,version=version+1,
                next_attempt_at=clock_timestamp(),completed_at=NULL,error_code=NULL WHERE id=%s""",
                (job_id,),
            )
            result: Final = await _job(connection, job_id, fingerprint)
            await self._record(
                connection,
                actor,
                key,
                action,
                digest,
                str(job_id),
                previous.version,
                result.version,
                {"state": "pending"},
                result,
            )
            return result

    async def discard(
        self,
        expected_version: int,
        confirmation: Literal["discard_pending"],
        actor: ControlPlaneActor,
        key: str,
        *,
        fingerprint: str | None = None,
    ) -> DeliveryStatus:
        require_admin(actor)
        _fingerprint(fingerprint)
        _version(expected_version)
        if confirmation != "discard_pending":
            raise AuditDeliveryError("invalid_confirmation", 422)
        action: Final = "audit_delivery.discarded"
        digest: Final = operation_digest(
            action, {"version": expected_version, "confirmation": confirmation, "configuration": fingerprint}
        )
        async with self._management_transaction() as connection:
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            if replay is not None:
                return DeliveryStatus.model_validate(replay)
            settings: Final = await _settings(connection)
            if settings.version != expected_version:
                raise AuditDeliveryError("settings_conflict")
            if settings.enabled:
                raise AuditDeliveryError("pause_required")
            changed: Final = await _many(
                connection,
                """UPDATE retail_audit_deliveries SET state='discarded',version=version+1,
                lease_owner=NULL,lease_token=NULL,lease_until=NULL,error_code='discarded',completed_at=clock_timestamp()
                WHERE state IN ('pending','processing','failed') RETURNING version""",
                (),
                _Version,
            )
            await connection.execute("UPDATE retail_audit_delivery_settings SET version=version+1 WHERE singleton")
            result: Final = await _status(connection, fingerprint)
            await self._record(
                connection,
                actor,
                key,
                action,
                digest,
                "settings",
                settings.version,
                result.config_version,
                {"state": "discarded", "row_count": len(changed)},
                result,
            )
            return result

    async def claim(self, owner: str, fingerprint: str) -> DeliveryClaim | None:
        _fingerprint(fingerprint)
        if not owner or len(owner) > 191:
            raise AuditDeliveryError("invalid_owner", 422)
        async with self._worker_transaction() as connection:
            settings: Final = await _settings(connection)
            if not settings.enabled or settings.config_fingerprint != fingerprint:
                return None
            await _recover(connection)
            active: Final = await _one(
                connection,
                "SELECT count(*) AS total FROM retail_audit_deliveries "
                "WHERE state='processing' AND lease_until>clock_timestamp()",
                (),
                _Count,
            )
            if active is not None and active.total >= 2:
                return None
            return await _one(
                connection,
                """WITH candidate AS (SELECT id FROM retail_audit_deliveries WHERE state='pending'
                AND config_fingerprint=%s AND attempts<5 AND next_attempt_at<=clock_timestamp()
                AND created_at>clock_timestamp()-interval '7 days'
                ORDER BY next_attempt_at,created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED)
                UPDATE retail_audit_deliveries j SET state='processing',attempts=attempts+1,version=version+1,
                lease_owner=%s,lease_token=gen_random_uuid(),lease_until=clock_timestamp()+interval '20 seconds'
                FROM candidate c WHERE j.id=c.id RETURNING j.id,j.event_id,j.config_fingerprint,j.lease_token,
                j.lease_owner,j.attempts,j.payload,j.lease_until""",
                (fingerprint, owner),
                DeliveryClaim,
            )

    async def complete(self, claim: DeliveryClaim) -> bool:
        async with self._worker_transaction() as connection:
            await _settings(connection)
            row: Final = await _one(
                connection,
                """UPDATE retail_audit_deliveries SET state='delivered',version=version+1,
                lease_owner=NULL,lease_token=NULL,lease_until=NULL,error_code=NULL,completed_at=clock_timestamp()
                WHERE id=%s AND state='processing' AND lease_owner=%s AND lease_token=%s
                  AND lease_until>clock_timestamp() RETURNING version""",
                (claim.id, claim.lease_owner, claim.lease_token),
                _Version,
            )
            return row is not None

    async def fail(self, claim: DeliveryClaim, error_code: DeliveryErrorCode, retryable: bool = True) -> bool:
        if error_code not in _ERRORS:
            raise AuditDeliveryError("invalid_failure_code", 422)
        async with self._worker_transaction() as connection:
            await _settings(connection)
            row: Final = await _one(
                connection,
                """UPDATE retail_audit_deliveries SET
                state=CASE WHEN %s AND attempts<5 THEN 'pending' ELSE 'failed' END,version=version+1,
                next_attempt_at=clock_timestamp()+(LEAST(300,power(2,attempts)) * interval '1 second'),
                lease_owner=NULL,lease_token=NULL,lease_until=NULL,error_code=%s,
                completed_at=CASE WHEN %s AND attempts<5 THEN NULL ELSE clock_timestamp() END
                WHERE id=%s AND state='processing' AND lease_owner=%s AND lease_token=%s
                  AND lease_until>clock_timestamp() RETURNING version""",
                (retryable, error_code, retryable, claim.id, claim.lease_owner, claim.lease_token),
                _Version,
            )
            return row is not None

    async def maintain(self) -> int:
        async with self._worker_transaction() as connection:
            settings: Final = await _settings(connection)
            recovered: Final = await _recover(connection)
            deleted: Final = await _many(
                connection,
                """WITH candidate AS (SELECT id FROM retail_audit_deliveries
                WHERE state IN ('delivered','failed','discarded') AND
                (completed_at<=clock_timestamp()-interval '7 days' OR %s>=8000)
                ORDER BY completed_at,id LIMIT 200 FOR UPDATE SKIP LOCKED)
                DELETE FROM retail_audit_deliveries j USING candidate c WHERE j.id=c.id RETURNING j.version""",
                (settings.retained_count,),
                _Version,
            )
            if deleted:
                await connection.execute(
                    "UPDATE retail_audit_delivery_settings SET retained_count=retained_count-%s WHERE singleton",
                    (len(deleted),),
                )
            return recovered + len(deleted)

    async def release_claims(self, owner: str) -> None:
        async with self._worker_transaction() as connection:
            await _settings(connection)
            await connection.execute(
                """UPDATE retail_audit_deliveries SET state=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,
                version=version+1,lease_owner=NULL,lease_token=NULL,lease_until=NULL,error_code='lease_expired',
                completed_at=CASE WHEN attempts>=5 THEN clock_timestamp() ELSE NULL END,
                next_attempt_at=clock_timestamp() WHERE state='processing' AND lease_owner=%s""",
                (owner,),
            )


@lru_cache(maxsize=4)
def get_audit_delivery_store(wallet: WalletStore | None = None) -> AuditDeliveryStore:
    return AuditDeliveryStore(wallet or get_store())
