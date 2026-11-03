import asyncio
import csv
import hashlib
import io
import json
import re
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal, TypeAlias
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from .database import database_integer_setting
from .models import nanos_to_amount
from .operation_events import append_operation_event
from .store import WalletStore, _many, _one, get_store

LedgerKind: TypeAlias = Literal["credit", "charge", "release"]
ReportState: TypeAlias = Literal["queued", "processing", "ready", "failed", "revoked", "expired"]


class ReportWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_at: AwareDatetime
    end_at: AwareDatetime
    kind: LedgerKind | None = None
    currency: Literal["USD"] = "USD"
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"

    @model_validator(mode="after")
    def ordered(self) -> "ReportWindow":
        if self.start_at >= self.end_at:
            raise ValueError("The report start must precede its exclusive end")
        return self


class ReportError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ReportLimits:
    max_rows: int = 10000
    max_bytes: int = 10 * 1024 * 1024
    max_days: int = 31
    expires_seconds: int = 86400
    max_actor_jobs: int = 20
    max_pending_jobs: int = 100
    snapshot_concurrency: int = 2
    worker_concurrency: int = 2
    lease_seconds: int = 60
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if not 1 <= self.max_rows <= 100000 or not 1024 <= self.max_bytes <= 25 * 1024 * 1024:
            raise ValueError("Invalid report size budget")
        if not 1 <= self.max_days <= 366 or not 60 <= self.expires_seconds <= 7 * 86400:
            raise ValueError("Invalid report time budget")
        if not 1 <= self.snapshot_concurrency <= 8 or not 1 <= self.worker_concurrency <= 8:
            raise ValueError("Invalid report concurrency")
        if min(self.max_actor_jobs, self.max_pending_jobs, self.max_attempts) < 1 or self.lease_seconds < 10:
            raise ValueError("Invalid report queue budget")


class ReportTotals(BaseModel):
    model_config = ConfigDict(frozen=True)

    credit_nanos: str
    charge_nanos: str
    release_nanos: str
    credit: str
    charge: str
    release: str


class ReportUnresolved(BaseModel):
    model_config = ConfigDict(frozen=True)

    pending: int = 0
    unknown: int = 0
    conflict: int = 0


class ReportJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    account_id: str
    scope: Literal["account"] = "account"
    state: ReportState
    start_at: AwareDatetime
    end_at: AwareDatetime
    snapshot_at: AwareDatetime
    created_at: AwareDatetime
    expires_at: AwareDatetime
    partial: bool
    currency: Literal["USD"] = "USD"
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    total_rows: int
    totals: ReportTotals
    unresolved: ReportUnresolved
    artifact_bytes: int = 0
    error_code: str | None = None


class ReportPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[ReportJob, ...]
    total: int
    limit: int
    offset: int


class _JobRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    creator_id: str
    account_id: str
    request_digest: str
    state: ReportState
    start_at: datetime
    end_at: datetime
    snapshot_at: datetime
    created_at: datetime
    expires_at: datetime
    partial: bool
    row_count: int
    summary: ReportTotals
    unresolved: ReportUnresolved
    artifact_bytes: int
    artifact_sha256: str | None
    error_code: str | None

    def public(self) -> ReportJob:
        return ReportJob(
            **self.model_dump(exclude={"creator_id", "request_digest", "row_count", "summary", "artifact_sha256"}),
            total_rows=self.row_count,
            totals=self.summary,
        )


class ReportLedgerRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ledger_id: UUID
    kind: LedgerKind
    amount_nanos: int
    balance_after_nanos: int
    request_id: str | None
    model: str | None
    created_at: datetime


class _ClockRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    value: datetime


class _CountRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    total: int


class _AccountRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    account_id: str


class _TotalsRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    credit: int
    charge: int
    release: int


class _CompletionRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    creator_id: str
    account_id: str
    row_count: int
    state: str


class _FailedJobRow(_CompletionRow):
    id: UUID


class _ArtifactRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    content: bytes


class ReportClaim(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    lease_owner: str


_JOB_COLUMNS: Final = """id,creator_id,account_id,request_digest,
CASE WHEN expires_at<=now() THEN 'expired' ELSE state END AS state,
start_at,end_at,snapshot_at,created_at,expires_at,partial,row_count,summary,unresolved,
artifact_bytes,artifact_sha256,error_code"""
_BEIJING: Final = ZoneInfo("Asia/Shanghai")
_CSV_HEADERS: Final = (
    "ledger_id",
    "created_at_utc",
    "created_at_beijing",
    "kind",
    "amount_nanos",
    "amount_usd",
    "balance_after_nanos",
    "balance_after_usd",
    "request_id",
    "model",
)


def report_digest(account_id: str, window: ReportWindow) -> str:
    payload: Final = {
        **window.model_dump(mode="json"),
        "account_id": account_id,
        "start_at": window.start_at.astimezone(timezone.utc).isoformat(),
        "end_at": window.end_at.astimezone(timezone.utc).isoformat(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_window(window: ReportWindow, now: datetime, limits: ReportLimits) -> None:
    if window.end_at - window.start_at > timedelta(days=limits.max_days):
        raise ReportError("window_too_large", 422)
    if window.start_at > now:
        raise ReportError("future_window", 422)


def open_day_window(window: ReportWindow, now: datetime) -> bool:
    day_start: Final = now.astimezone(_BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    return window.end_at > day_start


def totals_view(row: _TotalsRow) -> ReportTotals:
    return ReportTotals(
        credit_nanos=str(row.credit),
        charge_nanos=str(row.charge),
        release_nanos=str(row.release),
        credit=nanos_to_amount(row.credit),
        charge=nanos_to_amount(row.charge),
        release=nanos_to_amount(row.release),
    )


def csv_text(value: str | None) -> str:
    text: Final = value or ""
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def report_csv(rows: Sequence[ReportLedgerRow], limits: ReportLimits) -> bytes:
    if len(rows) > limits.max_rows:
        raise ReportError("row_limit_exceeded", 422)
    output: Final = io.StringIO(newline="")
    writer: Final = csv.writer(output, lineterminator="\r\n")
    writer.writerow(_CSV_HEADERS)
    for row in rows:
        writer.writerow(
            (
                str(row.ledger_id),
                row.created_at.astimezone(timezone.utc).isoformat(),
                row.created_at.astimezone(_BEIJING).isoformat(),
                row.kind,
                str(row.amount_nanos),
                nanos_to_amount(row.amount_nanos),
                str(row.balance_after_nanos),
                nanos_to_amount(row.balance_after_nanos),
                csv_text(row.request_id),
                csv_text(row.model),
            )
        )
        if output.tell() > limits.max_bytes:
            raise ReportError("artifact_too_large", 422)
    result: Final = output.getvalue().encode("utf-8")
    if len(result) > limits.max_bytes:
        raise ReportError("artifact_too_large", 422)
    return result


def byte_range(value: str | None, length: int) -> tuple[int, int] | None:
    if value is None:
        return None
    if len(value) > 128:
        raise ReportError("invalid_range", 416)
    match: Final = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or not any(match.groups()) or length < 1:
        raise ReportError("invalid_range", 416)
    left, right = match.groups()
    start: Final = int(left) if left else max(0, length - int(right))
    end: Final = min(int(right), length - 1) if left and right else length - 1
    if (not left and int(right) == 0) or start >= length or start > end:
        raise ReportError("invalid_range", 416)
    return start, end


def render_artifact(rows: Sequence[object], limits: ReportLimits) -> tuple[bytes, str]:
    validated: Final = tuple(ReportLedgerRow.model_validate(row) for row in rows)
    content: Final = report_csv(validated, limits)
    return content, hashlib.sha256(content).hexdigest()


class ReportStore:
    def __init__(self, database_url: str, limits: ReportLimits | None = None, *, connections: int = 2) -> None:
        if not 1 <= connections <= 4:
            raise ValueError("Report connections must be between one and four")
        self.limits = limits or ReportLimits()
        self._open_lock = asyncio.Lock()
        self._initialized = False
        self._render_slots = asyncio.Semaphore(self.limits.worker_concurrency)
        self._render_executor = ThreadPoolExecutor(
            max_workers=self.limits.worker_concurrency, thread_name_prefix="retail-report"
        )
        options: Final = conninfo_to_dict(database_url).get("options", "")
        self.pool = AsyncConnectionPool(
            database_url,
            open=False,
            min_size=0,
            max_size=connections,
            max_waiting=4,
            timeout=3,
            name="retail-reports",
            check=AsyncConnectionPool.check_connection,
            kwargs={
                "row_factory": dict_row,
                "autocommit": True,
                "connect_timeout": 3,
                "options": f"{options} -c statement_timeout=8000 -c lock_timeout=1500 "
                "-c idle_in_transaction_session_timeout=15000",
            },
        )

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._open_lock:
            if not self._initialized:
                await self.pool.open()
                async with self.pool.connection() as connection, connection.transaction():
                    await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('retail:report:schema',0))")
                    await connection.execute(
                        Path(__file__).with_name("report_schema.sql").read_text(encoding="utf-8"), prepare=False
                    )
                self._initialized = True

    async def close(self) -> None:
        await self.pool.close()
        self._render_executor.shutdown(wait=False, cancel_futures=True)

    async def render(self, rows: Sequence[object]) -> tuple[bytes, str]:
        await self._render_slots.acquire()
        try:
            pending: Final = asyncio.get_running_loop().run_in_executor(
                self._render_executor,
                render_artifact,
                rows,
                self.limits,
            )
        except RuntimeError:
            self._render_slots.release()
            raise
        pending.add_done_callback(self._render_finished)
        return await asyncio.shield(pending)

    def _render_finished(self, pending: asyncio.Future[tuple[bytes, str]]) -> None:
        self._render_slots.release()
        if not pending.cancelled():
            pending.exception()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        await self.initialize()
        async with self.pool.connection() as connection, connection.transaction():
            yield connection

    async def create(self, creator_id: str, account_id: str, operation_key: str, window: ReportWindow) -> ReportJob:
        for attempt in range(3):
            try:
                return await self._create_snapshot(creator_id, account_id, operation_key, window)
            except psycopg.errors.CheckViolation as exc:
                raise ReportError("row_projection_too_large", 422) from exc
            except (psycopg.errors.SerializationFailure, psycopg.errors.UniqueViolation):
                if attempt == 2:
                    raise ReportError("report_busy", 503) from None
        raise ReportError("report_busy", 503)

    async def _create_snapshot(
        self,
        creator_id: str,
        account_id: str,
        operation_key: str,
        window: ReportWindow,
    ) -> ReportJob:
        async with self.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("retail:report:" + creator_id,)
            )
            digest: Final = report_digest(account_id, window)
            previous: Final = await _one(
                connection,
                f"SELECT {_JOB_COLUMNS} FROM retail_report_jobs WHERE creator_id=%s AND operation_key=%s",
                (creator_id, operation_key),
                _JobRow,
            )
            if previous is not None:
                if previous.request_digest != digest:
                    raise ReportError("idempotency_conflict", 409)
                return previous.public()
            clock: Final = await _one(connection, "SELECT transaction_timestamp() AS value", (), _ClockRow)
            if clock is None:
                raise ReportError("report_unavailable", 503)
            validate_window(window, clock.value, self.limits)
            await self._snapshot_admission(connection, creator_id)
            wallet: Final = await _one(
                connection, "SELECT count(*) AS total FROM retail_wallets WHERE user_id=%s", (account_id,), _CountRow
            )
            if wallet is None or wallet.total != 1:
                raise ReportError("account_not_found", 404)
            report_id: Final = uuid4()
            empty_totals: Final = totals_view(_TotalsRow(credit=0, charge=0, release=0))
            await connection.execute(
                """INSERT INTO retail_report_jobs(id,creator_id,account_id,operation_key,
                request_digest,start_at,end_at,snapshot_at,kind,partial,expires_at,summary,unresolved)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    report_id,
                    creator_id,
                    account_id,
                    operation_key,
                    digest,
                    window.start_at,
                    window.end_at,
                    clock.value,
                    window.kind,
                    open_day_window(window, clock.value),
                    clock.value + timedelta(seconds=self.limits.expires_seconds),
                    Jsonb(empty_totals.model_dump()),
                    Jsonb(ReportUnresolved().model_dump()),
                ),
            )
            await connection.execute(
                """INSERT INTO retail_report_rows
                (report_id,sequence,ledger_id,kind,amount_nanos,balance_after_nanos,request_id,model,created_at)
                SELECT %s,row_number() OVER (ORDER BY created_at,id),id,kind,amount_nanos,
                  balance_after_nanos,request_id,model,created_at FROM (
                  SELECT id,kind,amount_nanos,balance_after_nanos,request_id,model,created_at FROM retail_ledger
                  WHERE user_id=%s AND created_at>=%s AND created_at<%s AND (%s::text IS NULL OR kind=%s)
                  ORDER BY created_at,id LIMIT %s) entries""",
                (
                    report_id,
                    account_id,
                    window.start_at,
                    window.end_at,
                    window.kind,
                    window.kind,
                    self.limits.max_rows + 1,
                ),
            )
            count: Final = await _one(
                connection,
                "SELECT count(*) AS total FROM retail_report_rows WHERE report_id=%s",
                (report_id,),
                _CountRow,
            )
            if count is None or count.total > self.limits.max_rows:
                raise ReportError("row_limit_exceeded", 422)
            totals: Final = await _one(
                connection,
                """SELECT
                COALESCE(sum(amount_nanos) FILTER(WHERE kind='credit'),0) AS credit,
                -COALESCE(sum(amount_nanos) FILTER(WHERE kind='charge'),0) AS charge,
                COALESCE(sum(amount_nanos) FILTER(WHERE kind='release'),0) AS release
                FROM retail_report_rows WHERE report_id=%s""",
                (report_id,),
                _TotalsRow,
            )
            unresolved: Final = await _one(
                connection,
                """SELECT
                count(*) FILTER(WHERE r.status='reserved' AND COALESCE(f.state,'') NOT IN ('unknown','conflict')
                    AND COALESCE(f.last_error_type,'')<>'IdempotencyConflict') AS pending,
                count(*) FILTER(WHERE f.state='unknown') AS unknown,
                count(*) FILTER(WHERE f.state='conflict' OR f.last_error_type='IdempotencyConflict') AS conflict
                FROM retail_reservations r LEFT JOIN retail_settlement_facts f ON f.request_id=r.request_id
                WHERE r.user_id=%s AND r.created_at>=%s AND r.created_at<%s""",
                (account_id, window.start_at, window.end_at),
                ReportUnresolved,
            )
            if totals is None or unresolved is None:
                raise ReportError("report_unavailable", 503)
            job: Final = await _one(
                connection,
                f"""UPDATE retail_report_jobs SET row_count=%s,summary=%s,unresolved=%s
                WHERE id=%s RETURNING {_JOB_COLUMNS}""",
                (count.total, Jsonb(totals_view(totals).model_dump()), Jsonb(unresolved.model_dump()), report_id),
                _JobRow,
            )
            if job is None:
                raise ReportError("report_unavailable", 503)
            await append_operation_event(
                connection,
                actor_id=creator_id,
                action="report.created",
                resource_type="report",
                resource_id=str(report_id),
                organization_id=None,
                version_before=None,
                version_after=None,
                changes={
                    "user_id": account_id,
                    "start": window.start_at.isoformat(),
                    "end": window.end_at.isoformat(),
                    "currency": "USD",
                    "row_count": count.total,
                    "format": "csv",
                    "state": "queued",
                },
                idempotency_key="report:create:" + hashlib.sha256(operation_key.encode()).hexdigest(),
                request_digest=digest,
                response={"report_id": str(report_id)},
            )
            return job.public()

    async def _snapshot_admission(self, connection: psycopg.AsyncConnection, creator_id: str) -> None:
        actor: Final = await _one(
            connection,
            """SELECT count(*) AS total FROM retail_report_jobs
            WHERE creator_id=%s AND created_at>now()-interval '1 day'""",
            (creator_id,),
            _CountRow,
        )
        pending: Final = await _one(
            connection,
            """SELECT count(*) AS total FROM retail_report_jobs
            WHERE state IN ('queued','processing') AND expires_at>now()""",
            (),
            _CountRow,
        )
        if actor is None or pending is None or actor.total >= self.limits.max_actor_jobs:
            raise ReportError("report_quota_reached", 429)
        if pending.total >= self.limits.max_pending_jobs:
            raise ReportError("report_busy", 503)
        for slot in range(self.limits.snapshot_concurrency):
            cursor: Final = await connection.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired",
                (f"retail:report:snapshot:{slot}",),
            )
            row: Final = await cursor.fetchone()
            if row is not None and row["acquired"] is True:
                return
        raise ReportError("report_busy", 503)

    async def get(self, report_id: UUID, creator_id: str, is_admin: bool = False) -> _JobRow:
        async with self.transaction() as connection:
            row: Final = await _one(
                connection,
                f"SELECT {_JOB_COLUMNS} FROM retail_report_jobs "
                "WHERE id=%s AND creator_id=%s AND (account_id=%s OR %s)",
                (report_id, creator_id, creator_id, is_admin),
                _JobRow,
            )
        if row is None:
            raise ReportError("report_not_found", 404)
        return row

    async def target_accounts(self, creator_id: str) -> tuple[str, ...]:
        async with self.transaction() as connection:
            rows: Final = await _many(
                connection,
                "SELECT DISTINCT account_id FROM retail_report_jobs WHERE creator_id=%s AND account_id<>%s LIMIT 1001",
                (creator_id, creator_id),
                _AccountRow,
            )
        if len(rows) > 1000:
            raise ReportError("report_busy", 503)
        return tuple(row.account_id for row in rows)

    async def list(
        self,
        creator_id: str,
        is_admin: bool,
        limit: int = 25,
        offset: int = 0,
        *,
        allowed_accounts: tuple[str, ...] | None = None,
    ) -> ReportPage:
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise ReportError("invalid_pagination", 422)
        accounts: Final = list(allowed_accounts if allowed_accounts is not None else (creator_id,))
        async with self.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            count: Final = await _one(
                connection,
                "SELECT count(*) AS total FROM retail_report_jobs "
                "WHERE creator_id=%s AND (account_id=%s OR %s) AND account_id=ANY(%s)",
                (creator_id, creator_id, is_admin, accounts),
                _CountRow,
            )
            rows: Final = await _many(
                connection,
                f"SELECT {_JOB_COLUMNS} FROM retail_report_jobs "
                "WHERE creator_id=%s AND (account_id=%s OR %s) AND account_id=ANY(%s) "
                "ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
                (creator_id, creator_id, is_admin, accounts, limit, offset),
                _JobRow,
            )
        return ReportPage(
            items=tuple(row.public() for row in rows), total=count.total if count else 0, limit=limit, offset=offset
        )

    async def claim(self, owner: str) -> ReportClaim | None:
        async with self.transaction() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('retail:report:claim',0))")
            active: Final = await _one(
                connection,
                """SELECT count(*) AS total FROM retail_report_jobs
                WHERE state='processing' AND lease_until>now() AND expires_at>now()""",
                (),
                _CountRow,
            )
            if active is None or active.total >= self.limits.worker_concurrency:
                return None
            return await _one(
                connection,
                """WITH candidate AS (
                SELECT id FROM retail_report_jobs WHERE expires_at>now() AND attempts<%s AND
                (state='queued' OR (state='processing' AND lease_until<=now()))
                ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE retail_report_jobs j SET state='processing',lease_owner=%s,
                lease_until=now()+(%s*interval '1 second'),attempts=attempts+1
                FROM candidate WHERE j.id=candidate.id RETURNING j.id,j.lease_owner""",
                (self.limits.max_attempts, owner, self.limits.lease_seconds),
                ReportClaim,
            )

    async def build(self, claim: ReportClaim) -> bool:
        async with self.transaction() as connection:
            cursor: Final = await connection.execute(
                """SELECT ledger_id,kind,amount_nanos,balance_after_nanos,
                request_id,model,created_at FROM retail_report_rows WHERE report_id=%s ORDER BY sequence LIMIT %s""",
                (claim.id, self.limits.max_rows + 1),
            )
            rows: Final = await cursor.fetchall()
        content, digest = await self.render(rows)
        async with self.transaction() as connection:
            completed: Final = await _one(
                connection,
                """UPDATE retail_report_jobs SET state='ready',artifact=%s,
                artifact_bytes=%s,artifact_sha256=%s,lease_owner=NULL,lease_until=NULL,error_code=NULL
                WHERE id=%s AND lease_owner=%s AND state='processing' AND lease_until>now() AND expires_at>now()
                RETURNING creator_id,account_id,row_count,state""",
                (content, len(content), digest, claim.id, claim.lease_owner),
                _CompletionRow,
            )
            if completed is None:
                return False
            await self._completion_event(connection, claim.id, completed, digest)
            return True

    async def _completion_event(
        self,
        connection: psycopg.AsyncConnection,
        report_id: UUID,
        row: _CompletionRow,
        digest: str,
    ) -> None:
        await append_operation_event(
            connection,
            actor_id="system:retail-report-worker",
            action="report." + ("completed" if row.state == "ready" else "failed"),
            resource_type="report",
            resource_id=str(report_id),
            organization_id=None,
            version_before=None,
            version_after=None,
            changes={"user_id": row.account_id, "row_count": row.row_count, "format": "csv", "state": row.state},
            idempotency_key=f"report:{row.state}:{report_id}",
            request_digest=digest,
            response={"report_id": str(report_id), "state": row.state},
        )

    async def retry(self, claim: ReportClaim, error_code: str | None = None) -> None:
        async with self.transaction() as connection:
            failed: Final = await _one(
                connection,
                """UPDATE retail_report_jobs SET
                state=CASE WHEN %s::text IS NOT NULL OR attempts>=%s THEN 'failed' ELSE 'queued' END,
                error_code=COALESCE(%s,CASE WHEN attempts>=%s THEN 'generation_failed' ELSE NULL END),
                lease_owner=NULL,lease_until=NULL WHERE id=%s AND lease_owner=%s AND state='processing' RETURNING creator_id,account_id,row_count,state""",
                (
                    error_code,
                    self.limits.max_attempts,
                    error_code,
                    self.limits.max_attempts,
                    claim.id,
                    claim.lease_owner,
                ),
                _CompletionRow,
            )
            if failed is not None and failed.state == "failed":
                await self._completion_event(
                    connection,
                    claim.id,
                    failed,
                    hashlib.sha256((error_code or "generation_failed").encode()).hexdigest(),
                )

    async def release_claims(self, owner: str) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                """UPDATE retail_report_jobs SET state='queued',lease_owner=NULL,lease_until=NULL
                WHERE state='processing' AND lease_owner=%s""",
                (owner,),
            )

    async def expire(self) -> None:
        async with self.transaction() as connection:
            await connection.execute("""WITH expired AS (SELECT id FROM retail_report_jobs
                WHERE expires_at<=now() AND state<>'expired' ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT 20),
                cleared AS (UPDATE retail_report_jobs j SET state='expired',artifact=NULL,artifact_bytes=0,
                artifact_sha256=NULL,lease_owner=NULL,lease_until=NULL FROM expired WHERE j.id=expired.id RETURNING j.id)
                DELETE FROM retail_report_rows WHERE report_id IN (SELECT id FROM cleared)""")
            failed: Final = await _many(
                connection,
                """UPDATE retail_report_jobs SET state='failed',error_code='generation_failed',
                lease_owner=NULL,lease_until=NULL WHERE state='processing' AND lease_until<=now() AND attempts>=%s
                RETURNING id,creator_id,account_id,row_count,state""",
                (self.limits.max_attempts,),
                _FailedJobRow,
            )
            for row in failed:
                await self._completion_event(connection, row.id, row, hashlib.sha256(b"generation_failed").hexdigest())
            await connection.execute("""DELETE FROM retail_report_jobs WHERE id IN (SELECT id FROM retail_report_jobs
                WHERE state='expired' AND expires_at<now()-interval '30 days'
                ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT 20)""")

    async def artifact(self, job: _JobRow, interval: tuple[int, int] | None) -> bytes:
        if job.state == "expired":
            raise ReportError("report_expired", 410)
        if job.state != "ready":
            raise ReportError("report_not_ready", 409)
        start, end = interval if interval is not None else (0, job.artifact_bytes - 1)
        async with self.transaction() as connection:
            row: Final = await _one(
                connection,
                """SELECT substring(artifact from %s for %s) AS content
                FROM retail_report_jobs WHERE id=%s AND state='ready' AND expires_at>now()""",
                (start + 1, end - start + 1, job.id),
                _ArtifactRow,
            )
        if row is None:
            raise ReportError("report_not_found", 404)
        return row.content


@lru_cache(maxsize=4)
def _store_for_url(database_url: str) -> ReportStore:
    limits: Final = ReportLimits(
        max_rows=database_integer_setting("RETAIL_REPORT_MAX_ROWS", 10000, 1, 100000),
        max_bytes=database_integer_setting("RETAIL_REPORT_MAX_BYTES", 10 * 1024 * 1024, 1024, 25 * 1024 * 1024),
        max_days=database_integer_setting("RETAIL_REPORT_MAX_DAYS", 31, 1, 366),
    )
    return ReportStore(
        database_url, limits, connections=database_integer_setting("RETAIL_REPORT_DB_CONNECTIONS", 2, 1, 4)
    )


def get_report_store(store: WalletStore | None = None) -> ReportStore:
    return _store_for_url((store or get_store()).database_url)


async def initialize_reports(store: WalletStore) -> None:
    await get_report_store(store).initialize()
