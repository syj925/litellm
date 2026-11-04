import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Final, Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict

from .delegated_report_csv import render_delegated_artifact
from .finance_grants import FINANCE_JOB_AUTHORIZATION_SQL, FinanceAuthority, require_finance
from .models import nanos_to_amount
from .operation_events import append_operation_event
from .organization_consumption import ORGANIZATION_COHORT_SQL
from .organization_finance_store import OrganizationConsumptionSummary, organization_identifier
from .organization_report_store import OrganizationReportWindow
from .report_store import ReportClaim, ReportError, ReportState, ReportStore, ReportWindow, open_day_window
from .store import _many, _one


class DelegatedReportJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    scope: Literal["delegated_organization"] = "delegated_organization"
    axis: Literal["admission"] = "admission"
    organization_id: str
    organization_name: str
    access_revision: UUID
    state: ReportState
    start_at: AwareDatetime
    end_at: AwareDatetime
    snapshot_at: AwareDatetime
    created_at: AwareDatetime
    expires_at: AwareDatetime
    partial: bool
    total_rows: int
    summary: OrganizationConsumptionSummary
    artifact_bytes: int = 0


class DelegatedReportPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[DelegatedReportJob, ...]
    total: int
    limit: int
    offset: int


class _DelegatedJobRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    creator_id: str
    organization_id: str
    organization_name: str
    grant_id: UUID
    grant_version: int
    grant_revision: UUID
    grant_member_version: int
    grant_org_generation: int
    request_digest: str
    state: ReportState
    start_at: datetime
    end_at: datetime
    snapshot_at: datetime
    created_at: datetime
    expires_at: datetime
    partial: bool
    row_count: int
    summary: OrganizationConsumptionSummary
    artifact_bytes: int
    artifact_sha256: str | None
    error_code: str | None

    def public(self) -> DelegatedReportJob:
        return DelegatedReportJob(
            **self.model_dump(exclude={
                "creator_id", "request_digest", "row_count", "artifact_sha256", "error_code", "grant_id", "grant_version",
                "grant_revision", "grant_member_version", "grant_org_generation",
            }), total_rows=self.row_count, access_revision=self.grant_revision,
        )

    def authority(self) -> FinanceAuthority:
        return FinanceAuthority(grant_id=self.grant_id, version=self.grant_version, revision=self.grant_revision,
                                member_version=self.grant_member_version, org_generation=self.grant_org_generation,
                                organization_id=self.organization_id, organization_name=self.organization_name,
                                user_id=self.creator_id, can_export=True)



class _Clock(BaseModel):
    model_config = ConfigDict(frozen=True)
    value: datetime


class _Summary(BaseModel):
    model_config = ConfigDict(frozen=True)
    accepted_requests: int
    settled_requests: int
    released_requests: int
    pending_requests: int
    unknown_requests: int
    conflict_requests: int
    settled_nanos: int

    def public(self) -> OrganizationConsumptionSummary:
        return OrganizationConsumptionSummary(
            **self.model_dump(exclude={"settled_nanos"}),
            settled_nanos=str(self.settled_nanos), settled_usd=nanos_to_amount(self.settled_nanos),
        )


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True)
    total: int


class _Completion(BaseModel):
    model_config = ConfigDict(frozen=True)
    creator_id: str
    organization_id: str
    row_count: int
    state: str


class _Failure(_Completion):
    id: UUID


class _Artifact(BaseModel):
    model_config = ConfigDict(frozen=True)
    content: bytes


_JOB_COLUMNS: Final = """id,creator_id,organization_id,organization_name,request_digest,
grant_id,grant_version,grant_revision,grant_member_version,grant_org_generation,
CASE WHEN expires_at<=now() THEN 'expired' ELSE state END AS state,
start_at,end_at,snapshot_at,created_at,expires_at,partial,row_count,summary,
artifact_bytes,artifact_sha256,error_code"""
_SUMMARY: Final = """SELECT count(*) AS accepted_requests,
  count(*) FILTER(WHERE state='settled') AS settled_requests,
  count(*) FILTER(WHERE state='released') AS released_requests,
  count(*) FILTER(WHERE state='pending') AS pending_requests,
  count(*) FILTER(WHERE state='unknown') AS unknown_requests,
  count(*) FILTER(WHERE state='conflict') AS conflict_requests,
  COALESCE(sum(settled_nanos),0) AS settled_nanos
  FROM retail_delegated_report_rows WHERE report_id=%s"""


def delegated_report_digest(organization_id: str, window: OrganizationReportWindow, authority: FinanceAuthority) -> str:
    payload: Final = {
        **window.model_dump(mode="json"), "organization_id": organization_id, "scope": "delegated_organization", "axis": "admission",
        "access_revision": str(authority.revision),
        "start_at": window.start_at.astimezone(timezone.utc).isoformat(),
        "end_at": window.end_at.astimezone(timezone.utc).isoformat(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DelegatedReportStore:
    def __init__(self, base: ReportStore) -> None:
        self.base = base

    async def _lock_scope(self, connection: psycopg.AsyncConnection, organization_id: str, user_id: str) -> None:
        await connection.execute("SELECT id FROM retail_organizations WHERE id=%s FOR SHARE", (organization_id,))
        await connection.execute("SELECT user_id FROM retail_organization_memberships "
                                 "WHERE organization_id=%s AND user_id=%s FOR SHARE", (organization_id, user_id))
        await connection.execute("SELECT grant_id FROM retail_organization_finance_grants "
                                 "WHERE organization_id=%s AND user_id=%s FOR SHARE", (organization_id, user_id))

    async def create(
        self, creator_id: str, organization_id: str, operation_key: str, window: OrganizationReportWindow
    ) -> DelegatedReportJob:
        organization_identifier(organization_id)
        for attempt in range(3):
            try:
                return await self._create_snapshot(creator_id, organization_id, operation_key, window)
            except psycopg.errors.CheckViolation as exc:
                if exc.diag.constraint_name in {
                    "retail_delegated_report_snapshot_consistent", "retail_delegated_report_rows_check",
                    "retail_delegated_report_rows_settled_nanos_check",
                }:
                    raise ReportError("snapshot_inconsistent", 503) from exc
                raise ReportError("row_projection_too_large", 422) from exc
            except (psycopg.errors.SerializationFailure, psycopg.errors.UniqueViolation):
                if attempt == 2:
                    raise ReportError("report_busy", 503) from None
        raise ReportError("report_busy", 503)

    async def _create_snapshot(
        self, creator_id: str, organization_id: str, operation_key: str, window: OrganizationReportWindow
    ) -> DelegatedReportJob:
        async with self.base.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("retail:report:" + creator_id,),
            )
            await self._lock_scope(connection, organization_id, creator_id)
            authority: Final = await require_finance(connection, organization_id, creator_id, export=True)
            digest: Final = delegated_report_digest(organization_id, window, authority)
            previous: Final = await _one(
                connection, f"SELECT {_JOB_COLUMNS} FROM retail_delegated_report_jobs WHERE creator_id=%s AND operation_key=%s",
                (creator_id, operation_key), _DelegatedJobRow,
            )
            if previous is not None:
                if previous.request_digest != digest or not authority.same_epoch(previous.authority()):
                    raise ReportError("idempotency_conflict", 409)
                return previous.public()
            clock: Final = await _one(connection, "SELECT transaction_timestamp() AS value", (), _Clock)
            if clock is None:
                raise ReportError("report_unavailable", 503)
            if window.end_at-window.start_at > timedelta(days=min(31, self.base.limits.max_days)):
                raise ReportError("window_too_large", 422)
            if window.start_at > clock.value:
                raise ReportError("future_window", 422)
            await self.base.snapshot_admission(connection, creator_id)
            report_id: Final = uuid4()
            await connection.execute(
                """INSERT INTO retail_delegated_report_jobs
                (id,creator_id,organization_id,organization_name,grant_id,grant_version,grant_revision,
                 grant_member_version,grant_org_generation,operation_key,request_digest,
                 start_at,end_at,snapshot_at,partial,expires_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (report_id, creator_id, organization_id, authority.organization_name,
                 authority.grant_id, authority.version, authority.revision, authority.member_version, authority.org_generation,
                 operation_key, digest,
                 window.start_at, window.end_at, clock.value,
                 open_day_window(ReportWindow.model_validate(window.model_dump()), clock.value),
                 clock.value+timedelta(seconds=self.base.limits.expires_seconds)),
            )
            await connection.execute(
                ORGANIZATION_COHORT_SQL + """ INSERT INTO retail_delegated_report_rows
                (report_id,sequence,model,captured_at,state,settled_nanos,inconsistent)
                SELECT %s,row_number() OVER(ORDER BY captured_at,request_id),model,captured_at,state,
                  CASE WHEN state='settled' THEN -charge_nanos END,inconsistent FROM cohort""",
                (organization_id, window.start_at, window.end_at, report_id),
            )
            summary: Final = await _one(connection, _SUMMARY, (report_id,), _Summary)
            if summary is None:
                raise ReportError("report_unavailable", 503)
            if summary.accepted_requests > min(10000, self.base.limits.max_rows):
                raise ReportError("row_limit_exceeded", 422)
            job: Final = await _one(
                connection, f"""UPDATE retail_delegated_report_jobs SET row_count=%s,summary=%s
                WHERE id=%s RETURNING {_JOB_COLUMNS}""",
                (summary.accepted_requests, Jsonb(summary.public().model_dump()), report_id), _DelegatedJobRow,
            )
            if job is None:
                raise ReportError("report_unavailable", 503)
            await self._event(connection, report_id, _Completion.model_validate(job.model_dump()),
                              "delegated-report:create:"+hashlib.sha256(operation_key.encode()).hexdigest(), digest,
                              creator_id=creator_id, created=True)
            return job.public()

    async def get(self, report_id: UUID, creator_id: str, organization_id: str) -> _DelegatedJobRow:
        async with self.base.transaction() as connection:
            await require_finance(connection, organization_id, creator_id, export=True)
            row: Final = await _one(
                connection, f"SELECT {_JOB_COLUMNS} FROM retail_delegated_report_jobs j "
                "WHERE id=%s AND creator_id=%s AND organization_id=%s "
                "AND "+FINANCE_JOB_AUTHORIZATION_SQL,
                (report_id, creator_id, organization_id), _DelegatedJobRow,
            )
        if row is None:
            raise ReportError("report_not_found", 404)
        return row

    async def list(
        self, creator_id: str, organization_id: str, limit: int = 25, offset: int = 0
    ) -> DelegatedReportPage:
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise ReportError("invalid_pagination", 422)
        async with self.base.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            await require_finance(connection, organization_id, creator_id, export=True)
            count: Final = await _one(connection, "SELECT count(*) AS total FROM retail_delegated_report_jobs j "
                                     "WHERE creator_id=%s AND organization_id=%s AND "+FINANCE_JOB_AUTHORIZATION_SQL,
                                     (creator_id, organization_id), _Count)
            rows: Final = await _many(
                connection, f"SELECT {_JOB_COLUMNS} FROM retail_delegated_report_jobs j "
                "WHERE creator_id=%s AND organization_id=%s AND "+FINANCE_JOB_AUTHORIZATION_SQL+
                " ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
                (creator_id, organization_id, limit, offset), _DelegatedJobRow,
            )
        return DelegatedReportPage(items=tuple(row.public() for row in rows), total=count.total if count else 0,
                                      limit=limit, offset=offset)

    async def claim(self, owner: str) -> ReportClaim | None:
        async with self.base.transaction() as connection:
            if not await self.base.claim_admission(connection):
                return None
            return await _one(
                connection, """WITH candidate AS (
                SELECT id FROM retail_delegated_report_jobs WHERE expires_at>now() AND attempts<%s AND
                (state='queued' OR (state='processing' AND lease_until<=now()))
                ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE retail_delegated_report_jobs j SET state='processing',lease_owner=%s,
                lease_until=now()+(%s*interval '1 second'),attempts=attempts+1
                FROM candidate WHERE j.id=candidate.id RETURNING j.id,j.lease_owner""",
                (self.base.limits.max_attempts, owner, self.base.limits.lease_seconds), ReportClaim,
            )

    async def build(self, claim: ReportClaim) -> bool:
        async with self.base.transaction() as connection:
            cursor: Final = await connection.execute(
                "SELECT sequence,model,captured_at,state,settled_nanos FROM retail_delegated_report_rows "
                "WHERE report_id=%s ORDER BY sequence LIMIT %s", (claim.id, min(10000, self.base.limits.max_rows)+1),
            )
            rows: Final = await cursor.fetchall()
        content, digest = await self.base.render_with(rows, render_delegated_artifact)
        async with self.base.transaction() as connection:
            completed: Final = await _one(
                connection, """UPDATE retail_delegated_report_jobs SET state='ready',artifact=%s,
                artifact_bytes=%s,artifact_sha256=%s,lease_owner=NULL,lease_until=NULL,error_code=NULL
                WHERE id=%s AND lease_owner=%s AND state='processing' AND lease_until>now() AND expires_at>now()
                RETURNING creator_id,organization_id,row_count,state""",
                (content, len(content), digest, claim.id, claim.lease_owner), _Completion,
            )
            if completed is None:
                return False
            await self._event(connection, claim.id, completed, f"delegated-report:ready:{claim.id}", digest)
            return True

    async def _event(
        self, connection: psycopg.AsyncConnection, report_id: UUID, row: _Completion, key: str, digest: str,
        *, creator_id: str = "system:retail-report-worker", created: bool = False,
    ) -> None:
        await append_operation_event(
            connection, actor_id=creator_id,
            action="delegated_report."+("created" if created else "completed" if row.state == "ready" else "failed"),
            resource_type="delegated_report", resource_id=str(report_id), organization_id=None,
            version_before=None, version_after=None,
            changes={"organization_id": row.organization_id, "row_count": row.row_count, "format": "csv", "state": row.state},
            idempotency_key=key, request_digest=digest, response={"report_id": str(report_id), "state": row.state},
        )

    async def retry(self, claim: ReportClaim, error_code: str | None = None) -> None:
        async with self.base.transaction() as connection:
            failed: Final = await _one(
                connection, """UPDATE retail_delegated_report_jobs SET
                state=CASE WHEN %s::text IS NOT NULL OR attempts>=%s THEN 'failed' ELSE 'queued' END,
                error_code=COALESCE(%s,CASE WHEN attempts>=%s THEN 'generation_failed' ELSE NULL END),
                lease_owner=NULL,lease_until=NULL WHERE id=%s AND lease_owner=%s AND state='processing'
                RETURNING creator_id,organization_id,row_count,state""",
                (error_code, self.base.limits.max_attempts, error_code, self.base.limits.max_attempts,
                 claim.id, claim.lease_owner), _Completion,
            )
            if failed is not None and failed.state == "failed":
                await self._event(connection, claim.id, failed, f"delegated-report:failed:{claim.id}",
                                  hashlib.sha256((error_code or "generation_failed").encode()).hexdigest())

    async def release_claims(self, owner: str) -> None:
        async with self.base.transaction() as connection:
            await connection.execute("""UPDATE retail_delegated_report_jobs
                SET state='queued',lease_owner=NULL,lease_until=NULL WHERE state='processing' AND lease_owner=%s""", (owner,))

    async def expire(self) -> None:
        async with self.base.transaction() as connection:
            await connection.execute("""WITH expired AS (SELECT id FROM retail_delegated_report_jobs
                WHERE expires_at<=now() AND state<>'expired' ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT 20),
                cleared AS (UPDATE retail_delegated_report_jobs j SET state='expired',artifact=NULL,artifact_bytes=0,
                artifact_sha256=NULL,lease_owner=NULL,lease_until=NULL FROM expired WHERE j.id=expired.id RETURNING j.id)
                DELETE FROM retail_delegated_report_rows WHERE report_id IN (SELECT id FROM cleared)""")
            failed: Final = await _many(connection, """UPDATE retail_delegated_report_jobs
                SET state='failed',error_code='generation_failed',lease_owner=NULL,lease_until=NULL
                WHERE state='processing' AND lease_until<=now() AND attempts>=%s
                RETURNING id,creator_id,organization_id,row_count,state""", (self.base.limits.max_attempts,), _Failure)
            for row in failed:
                await self._event(connection, row.id, row, f"delegated-report:failed:{row.id}",
                                  hashlib.sha256(b"generation_failed").hexdigest())
            await connection.execute("""DELETE FROM retail_delegated_report_jobs WHERE id IN (
                SELECT id FROM retail_delegated_report_jobs WHERE state='expired'
                AND expires_at<now()-interval '30 days' ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT 20)""")

    async def artifact(self, job: _DelegatedJobRow, interval: tuple[int, int] | None) -> bytes:
        async with self.base.transaction() as connection:
            await require_finance(connection, job.organization_id, job.creator_id, export=True, expected=job.authority())
            if job.state == "expired":
                raise ReportError("report_expired", 410)
            if job.state != "ready":
                raise ReportError("report_not_ready", 409)
            start, end = interval if interval is not None else (0, job.artifact_bytes-1)
            row: Final = await _one(
                connection, """SELECT substring(artifact from %s for %s) AS content FROM retail_delegated_report_jobs j
                WHERE id=%s AND creator_id=%s AND organization_id=%s AND state='ready' AND expires_at>now()
                AND """+FINANCE_JOB_AUTHORIZATION_SQL,
                (start+1, end-start+1, job.id, job.creator_id, job.organization_id), _Artifact,
            )
        if row is None:
            raise ReportError("report_not_found", 404)
        return row.content
