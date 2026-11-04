import asyncio
import csv
import io
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from litellm.proxy.retail.organization_report_store import OrganizationReportStore, OrganizationReportWindow
from litellm.proxy.retail.report_store import ReportError, ReportLimits, ReportStore, ReportWindow
from litellm.proxy.retail.report_worker import ReportWorker
from litellm.proxy.retail.settlement_store import record_facts
from litellm.proxy.retail.settlement_worker import SettlementWorker
from litellm.proxy.retail.store import WalletStore


@dataclass(frozen=True, slots=True)
class Runtime:
    wallet: WalletStore
    base: ReportStore
    organizations: OrganizationReportStore
    window: OrganizationReportWindow


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated organization-report PostgreSQL service is required")
    for flag in ("RETAIL_OPERATION_AUDIT_ENABLED", "RETAIL_ORGANIZATIONS_ENABLED", "RETAIL_ORG_FINANCE_ENABLED", "RETAIL_REPORT_EXPORT_ENABLED"):
        monkeypatch.setenv(flag, "true")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_org_reports_"+uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    address: Final = make_conninfo(**{**base, "dbname": name})
    wallet: Final = WalletStore(address, max_connections=2, diagnostic_connections=2, critical_connections=1)
    reports: Final = ReportStore(address, ReportLimits(max_rows=100000, max_days=366), connections=4)
    try:
        async with await psycopg.AsyncConnection.connect(address) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        await wallet.initialize()
        await wallet.initialize_control_plane()
        await reports.initialize()
        await wallet.get_wallet("owner", create=True)
        await wallet.credit("owner", "1000", "seed", "fixture", "fixture", "seed")
        async with wallet.diagnostic_transaction() as connection:
            await connection.execute("INSERT INTO retail_organizations(id,name) VALUES('org-a','Original A'),('org-b','B')")
            await connection.execute("""INSERT INTO retail_organization_memberships(organization_id,user_id,role,approved_by)
                VALUES('org-a','owner','member','admin'),('org-b','owner','member','admin')""")
            await connection.execute("""INSERT INTO retail_billing_organization_assignments
                (user_id,organization_id,created_by,updated_by) VALUES('owner','org-a','admin','admin')""")
        now: Final = datetime.now(timezone.utc)
        yield Runtime(wallet, reports, OrganizationReportStore(reports), OrganizationReportWindow(
            start_at=now-timedelta(days=1), end_at=now+timedelta(minutes=10),
        ))
    finally:
        await reports.close()
        await wallet.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def reserve(runtime: Runtime, request_id: str) -> None:
    await runtime.wallet.reserve("owner", request_id, "public-model", "0.4", {}, max_parallel=100)


async def account_report(runtime: Runtime, key: str, creator: str = "admin") -> None:
    await runtime.base.create(creator, "owner", key, ReportWindow.model_validate(runtime.window.model_dump()))


async def financial_state(runtime: Runtime) -> str:
    async with runtime.base.transaction() as connection:
        row: Final = await (await connection.execute("""SELECT
            (SELECT jsonb_agg(to_jsonb(w) ORDER BY user_id) FROM retail_wallets w) AS wallets,
            (SELECT jsonb_agg(to_jsonb(r) ORDER BY request_id) FROM retail_reservations r) AS reservations,
            (SELECT jsonb_agg(to_jsonb(l) ORDER BY id) FROM retail_ledger l) AS ledger,
            (SELECT jsonb_agg(to_jsonb(s) ORDER BY request_id) FROM retail_request_organization_snapshots s) AS attributions""")).fetchone()
    return json.dumps(row, sort_keys=True)


async def queue_counts(runtime: Runtime) -> object:
    async with runtime.base.transaction() as connection:
        return await (await connection.execute("""SELECT
            (SELECT count(*) FROM retail_organization_report_jobs) AS jobs,
            (SELECT count(*) FROM retail_organization_report_rows) AS rows,
            (SELECT count(*) FROM retail_operation_events WHERE action='organization_report.created') AS created""")).fetchone()


@pytest.mark.asyncio
async def test_admission_snapshot_freezes_money_states_names_and_late_settlement_without_finance_writes(runtime: Runtime) -> None:
    for request in ("settled", "pending", "unknown", "released", "conflict"):
        await reserve(runtime, request)
    await runtime.wallet.settle("settled", "0.25", {})
    await record_facts(runtime.wallet, "unknown", None, {"billing_error": "missing"})
    await runtime.wallet.release("released", "upstream rejected")
    await runtime.wallet.release("conflict", "reviewed", {
        "actor_id": "admin", "reason": "reviewed", "reference": "manual", "idempotency_key": "manual",
    })
    await record_facts(runtime.wallet, "conflict", "0.2", {"usage": "late"})
    await SettlementWorker(runtime.wallet).run_once()
    before: Final = await financial_state(runtime)
    job: Final = await runtime.organizations.create("admin", "org-a", "fixed", runtime.window)
    assert await financial_state(runtime) == before
    assert job.total_rows == job.summary.accepted_requests == 5
    assert job.summary.settled_nanos == "250000000"
    assert (job.summary.settled_requests, job.summary.pending_requests, job.summary.unknown_requests,
            job.summary.released_requests, job.summary.conflict_requests) == (1, 1, 1, 1, 1)
    await runtime.wallet.settle("pending", "0.1", {})
    await runtime.wallet.settle("unknown", "0.12", {})
    await runtime.wallet.settle("released", "0.11", {})
    await reserve(runtime, "arrived-later")
    async with runtime.base.transaction() as connection:
        await connection.execute("UPDATE retail_organizations SET name='Renamed A',version=version+1 WHERE id='org-a'")
        await connection.execute("UPDATE retail_billing_organization_assignments SET organization_id='org-b',version=version+1")
    await reserve(runtime, "other-organization")
    claim: Final = await runtime.organizations.claim("fixture-worker")
    assert claim is not None and await runtime.organizations.build(claim)
    saved: Final = await runtime.organizations.get(job.id, "admin", "org-a")
    content: Final = await runtime.organizations.artifact(saved, None)
    exported: Final = {row["request_id"]: row for row in csv.DictReader(io.StringIO(content.decode()))}
    assert set(exported) == {"settled", "pending", "unknown", "released", "conflict"}
    assert exported["settled"]["settled_nanos"] == "250000000"
    assert all(exported[name]["settled_nanos"] == exported[name]["settled_usd"] == ""
               for name in ("pending", "unknown", "released", "conflict"))
    assert saved.summary == job.summary and saved.organization_name == "Original A"
    newer: Final = await runtime.organizations.create("admin", "org-a", "newer", runtime.window)
    assert newer.total_rows == 6 and newer.summary.settled_nanos == "580000000"
    assert newer.organization_name == "Renamed A"
    assert await runtime.organizations.artifact(saved, (0, 20)) == content[:21]


@pytest.mark.asyncio
async def test_concurrent_create_replay_has_one_fixed_snapshot_one_event_and_scope_sensitive_digest(runtime: Runtime) -> None:
    await reserve(runtime, "one")
    jobs: Final = await asyncio.gather(*(runtime.organizations.create("admin", "org-a", "same", runtime.window) for _ in range(4)))
    assert len({job.id for job in jobs}) == 1
    assert await queue_counts(runtime) == {"jobs": 1, "rows": 1, "created": 1}
    with pytest.raises(ReportError, match="idempotency_conflict"):
        await runtime.organizations.create("admin", "org-b", "same", runtime.window)
    await account_report(runtime, "same")
    assert await queue_counts(runtime) == {"jobs": 1, "rows": 1, "created": 1}


@pytest.mark.asyncio
async def test_creator_url_organization_and_current_enabled_state_are_checked_for_private_bytes(runtime: Runtime) -> None:
    job: Final = await runtime.organizations.create("admin", "org-a", "private", runtime.window)
    claim: Final = await runtime.organizations.claim("worker")
    assert claim is not None and await runtime.organizations.build(claim)
    saved: Final = await runtime.organizations.get(job.id, "admin", "org-a")
    with pytest.raises(ReportError, match="report_not_found"):
        await runtime.organizations.get(job.id, "other-admin", "org-a")
    with pytest.raises(ReportError, match="report_not_found"):
        await runtime.organizations.get(job.id, "admin", "org-b")
    assert (await runtime.organizations.list("other-admin", "org-a")).total == 0
    async with runtime.base.transaction() as connection:
        await connection.execute("UPDATE retail_organizations SET enabled=false WHERE id='org-a'")
    for read in (runtime.organizations.get(job.id, "admin", "org-a"), runtime.organizations.list("admin", "org-a"),
                 runtime.organizations.create("admin", "org-a", "private", runtime.window)):
        with pytest.raises(ReportError, match="organization_not_found"):
            await read
    with pytest.raises(ReportError, match="report_not_found"):
        await runtime.organizations.artifact(saved, (0, 2))


@pytest.mark.asyncio
async def test_hard_window_and_request_limits_cannot_be_raised_by_account_export_settings(runtime: Runtime) -> None:
    for begin, finish, error in ((runtime.window.start_at, runtime.window.start_at+timedelta(days=32), "window_too_large"),
                                 (runtime.window.end_at, runtime.window.end_at+timedelta(days=1), "future_window")):
        with pytest.raises(ReportError, match=error):
            await runtime.organizations.create("admin", "org-a", error, OrganizationReportWindow(start_at=begin, end_at=finish))
    async with runtime.base.transaction() as connection:
        await connection.execute("""INSERT INTO retail_reservations(request_id,user_id,model,reserved_nanos,price_snapshot)
            SELECT 'bulk-'||g,'owner','public-model',0,'{}'::jsonb FROM generate_series(1,10001) g""")
        await connection.execute("""INSERT INTO retail_request_organization_snapshots
            (request_id,organization_id,assignment_version,organization_version,membership_version,captured_at)
            SELECT request_id,'org-a',1,1,1,created_at FROM retail_reservations""")
    with pytest.raises(ReportError, match="row_limit_exceeded"):
        await runtime.organizations.create("admin", "org-a", "too-many", runtime.window)
    assert await queue_counts(runtime) == {"jobs": 0, "rows": 0, "created": 0}


@pytest.mark.parametrize("corruption", ("settled_without_charge", "positive_charge", "identifier_too_large"))
@pytest.mark.asyncio
async def test_inconsistent_or_oversized_projection_rolls_back_the_entire_job(runtime: Runtime, corruption: str) -> None:
    await reserve(runtime, "bad")
    if corruption == "positive_charge":
        await runtime.wallet.settle("bad", "0.2", {})
    async with runtime.base.transaction() as connection:
        if corruption == "settled_without_charge":
            await connection.execute("UPDATE retail_reservations SET status='settled' WHERE request_id='bad'")
        elif corruption == "positive_charge":
            await connection.execute("UPDATE retail_ledger SET amount_nanos=1 WHERE operation_key='charge:bad'")
        else:
            await connection.execute("UPDATE retail_reservations SET model=%s WHERE request_id='bad'", ("m"*1025,))
    expected: Final = "row_projection_too_large" if corruption == "identifier_too_large" else "snapshot_inconsistent"
    with pytest.raises(ReportError, match=expected):
        await runtime.organizations.create("admin", "org-a", "bad", runtime.window)
    assert await queue_counts(runtime) == {"jobs": 0, "rows": 0, "created": 0}


@pytest.mark.asyncio
async def test_audit_failure_cannot_commit_a_job_or_part_of_its_request_snapshot(runtime: Runtime) -> None:
    await reserve(runtime, "one")
    async with runtime.base.transaction() as connection:
        await connection.execute("""CREATE FUNCTION fail_organization_report_event() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.action='organization_report.created' THEN RAISE EXCEPTION 'fixture audit outage'; END IF;
            RETURN NEW; END; $$;
            CREATE TRIGGER fail_organization_report_event BEFORE INSERT ON retail_operation_events
            FOR EACH ROW EXECUTE FUNCTION fail_organization_report_event()""", prepare=False)
    with pytest.raises(psycopg.errors.RaiseException, match="fixture audit outage"):
        await runtime.organizations.create("admin", "org-a", "audit-failed", runtime.window)
    assert await queue_counts(runtime) == {"jobs": 0, "rows": 0, "created": 0}


@pytest.mark.asyncio
async def test_two_queues_share_global_active_worker_slots(runtime: Runtime) -> None:
    await account_report(runtime, "account")
    await runtime.organizations.create("admin", "org-a", "organization-1", runtime.window)
    await runtime.organizations.create("admin", "org-a", "organization-2", runtime.window)
    account: Final = await runtime.base.claim("account-worker")
    organization: Final = await runtime.organizations.claim("organization-worker")
    assert account is not None and organization is not None
    assert await runtime.organizations.claim("overflow") is None
    assert await runtime.base.claim("overflow") is None
    assert await runtime.base.build(account)
    assert await runtime.organizations.claim("available") is not None


@pytest.mark.asyncio
async def test_daily_actor_quota_counts_account_and_organization_jobs_together(runtime: Runtime) -> None:
    for index in range(10):
        await account_report(runtime, "account-"+str(index))
        await runtime.organizations.create("admin", "org-a", "organization-"+str(index), runtime.window)
    with pytest.raises(ReportError, match="report_quota_reached"):
        await runtime.organizations.create("admin", "org-a", "overflow", runtime.window)
    with pytest.raises(ReportError, match="report_quota_reached"):
        await account_report(runtime, "overflow")
    assert (await runtime.organizations.list("admin", "org-a")).total == 10


@pytest.mark.asyncio
async def test_global_pending_quota_counts_both_durable_queues(runtime: Runtime) -> None:
    await account_report(runtime, "account-seed")
    await runtime.organizations.create("admin", "org-a", "org-seed", runtime.window)
    async with runtime.base.transaction() as connection:
        await connection.execute("""INSERT INTO retail_report_jobs
            (id,creator_id,account_id,operation_key,request_digest,start_at,end_at,snapshot_at,partial,expires_at,summary,unresolved)
            SELECT gen_random_uuid(),'account-fixture-'||g,account_id,'fixture','digest',start_at,end_at,snapshot_at,
            partial,expires_at,summary,unresolved FROM retail_report_jobs CROSS JOIN generate_series(1,49) g""")
        await connection.execute("""INSERT INTO retail_organization_report_jobs
            (id,creator_id,organization_id,organization_name,operation_key,request_digest,start_at,end_at,snapshot_at,partial,expires_at,summary)
            SELECT gen_random_uuid(),'org-fixture-'||g,organization_id,organization_name,'fixture','digest',start_at,end_at,snapshot_at,
            partial,expires_at,summary FROM retail_organization_report_jobs CROSS JOIN generate_series(1,49) g""")
    with pytest.raises(ReportError, match="report_busy"):
        await runtime.organizations.create("new-admin", "org-a", "overflow", runtime.window)
    with pytest.raises(ReportError, match="report_busy"):
        await account_report(runtime, "overflow", "new-admin")


@pytest.mark.asyncio
async def test_snapshot_advisory_slots_are_shared_without_borrowing_wallet_connections(runtime: Runtime) -> None:
    assert runtime.organizations.base is runtime.base
    async with runtime.base.transaction() as first, runtime.base.transaction() as second:
        await runtime.base.snapshot_admission(first, "snapshot-1")
        await runtime.base.snapshot_admission(second, "snapshot-2")
        with pytest.raises(ReportError, match="report_busy"):
            await runtime.organizations.create("admin", "org-a", "overflow", runtime.window)
        with pytest.raises(ReportError, match="report_busy"):
            await account_report(runtime, "overflow")
    async with runtime.wallet.transaction(), runtime.wallet.transaction(), runtime.wallet.critical_transaction():
        assert (await runtime.organizations.create("admin", "org-a", "independent", runtime.window)).state == "queued"


@pytest.mark.asyncio
async def test_actual_worker_alternates_queues_and_preserves_account_csv_contract(runtime: Runtime) -> None:
    for index in range(2):
        await account_report(runtime, "account-"+str(index))
        await runtime.organizations.create("admin", "org-a", "org-"+str(index), runtime.window)
    worker: Final = ReportWorker(runtime.base)
    assert worker.organization_store.base is runtime.base
    assert await worker.run_once() == 1
    assert await worker.run_once() == 1
    async with runtime.base.transaction() as connection:
        ready: Final = await (await connection.execute("""SELECT
            (SELECT count(*) FROM retail_report_jobs WHERE state='ready') AS accounts,
            (SELECT count(*) FROM retail_organization_report_jobs WHERE state='ready') AS organizations""")).fetchone()
    assert ready == {"accounts": 1, "organizations": 1}
    assert await worker.run_once() == await worker.run_once() == 1
    page: Final = await runtime.base.list("admin", True, allowed_accounts=("owner",))
    saved: Final = await runtime.base.get(page.items[0].id, "admin", True)
    account_bytes: Final = await runtime.base.artifact(saved, None)
    exported: Final = tuple(csv.DictReader(io.StringIO(account_bytes.decode())))
    assert exported and exported[0]["kind"] == "credit" and exported[0]["request_id"] == ""
    assert "ledger_id" in exported[0] and "captured_at_utc" not in exported[0]


@pytest.mark.asyncio
async def test_off_flags_pause_organization_claims_and_report_flag_pauses_all_work(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    organization: Final = await runtime.organizations.create("admin", "org-a", "org", runtime.window)
    await account_report(runtime, "account")
    worker: Final = ReportWorker(runtime.base)
    monkeypatch.setenv("RETAIL_REPORT_EXPORT_ENABLED", "false")
    assert await worker.run_once() == 0
    monkeypatch.setenv("RETAIL_REPORT_EXPORT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORG_FINANCE_ENABLED", "false")
    assert await worker.run_once() == 1
    assert await worker.run_once() == 0
    assert (await runtime.organizations.get(organization.id, "admin", "org-a")).state == "queued"
    monkeypatch.setenv("RETAIL_ORG_FINANCE_ENABLED", "true")
    assert await worker.run_once() == 1
    assert (await runtime.organizations.get(organization.id, "admin", "org-a")).state == "ready"


@pytest.mark.asyncio
async def test_abandoned_lease_restarts_from_frozen_rows_and_fences_stale_completion(runtime: Runtime) -> None:
    await reserve(runtime, "pending")
    job: Final = await runtime.organizations.create("admin", "org-a", "recover", runtime.window)
    abandoned: Final = await runtime.organizations.claim("gone-worker")
    assert abandoned is not None
    async with runtime.base.transaction() as connection:
        await connection.execute("UPDATE retail_organization_report_jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (job.id,))
    await runtime.wallet.settle("pending", "0.1", {})
    restarted: Final = ReportWorker(runtime.base)
    assert await restarted.run_once() == 1
    assert await runtime.organizations.build(abandoned) is False
    ready: Final = await runtime.organizations.get(job.id, "admin", "org-a")
    exported: Final = tuple(csv.DictReader(io.StringIO((await runtime.organizations.artifact(ready, None)).decode())))
    assert exported[0]["state"] == "pending" and exported[0]["settled_nanos"] == ""
    async with runtime.base.transaction() as connection:
        completed: Final = await (await connection.execute("SELECT count(*) AS total FROM retail_operation_events WHERE action='organization_report.completed'")).fetchone()
    assert completed == {"total": 1}


@pytest.mark.asyncio
async def test_expiry_clears_organization_rows_and_private_artifact(runtime: Runtime) -> None:
    await reserve(runtime, "one")
    job: Final = await runtime.organizations.create("admin", "org-a", "expire", runtime.window)
    claim: Final = await runtime.organizations.claim("worker")
    assert claim is not None and await runtime.organizations.build(claim)
    async with runtime.base.transaction() as connection:
        await connection.execute("UPDATE retail_organization_report_jobs SET expires_at=now()-interval '1 second' WHERE id=%s", (job.id,))
    await runtime.organizations.expire()
    expired: Final = await runtime.organizations.get(job.id, "admin", "org-a")
    assert expired.state == "expired" and expired.artifact_bytes == 0 and expired.artifact_sha256 is None
    assert await queue_counts(runtime) == {"jobs": 1, "rows": 0, "created": 1}
    with pytest.raises(ReportError, match="report_expired"):
        await runtime.organizations.artifact(expired, None)
