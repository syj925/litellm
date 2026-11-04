import asyncio
import csv
import io
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import JsonValue, TypeAdapter, ValidationError

from litellm.proxy.retail.control_plane_access import ControlPlaneActor
from litellm.proxy.retail.delegated_report_store import DelegatedReportStore
from litellm.proxy.retail.finance_grants import FinanceGrantStore, FinanceGrantUpdate, FinanceGrantView, require_finance
from litellm.proxy.retail.organization_report_store import OrganizationReportStore, OrganizationReportWindow
from litellm.proxy.retail.organization_store import (
    MembershipRow,
    MemberUpdate,
    OrganizationRow,
    OrganizationStore,
    OrganizationUpdate,
)
from litellm.proxy.retail.report_store import ReportError, ReportLimits, ReportStore, ReportWindow
from litellm.proxy.retail.report_worker import ReportWorker
from litellm.proxy.retail.store import WalletStore

ROOT: Final = ControlPlaneActor("root", "proxy_admin", True, True)
DELEGATE: Final = "grantee@example.test"
CUSTOMER: Final = "private-customer@example.test"
_ITEMS: Final = TypeAdapter(tuple[Mapping[str, JsonValue], ...])


@dataclass(frozen=True, slots=True)
class Runtime:
    wallet: WalletStore
    base: ReportStore
    grants: FinanceGrantStore
    reports: DelegatedReportStore
    organizations: OrganizationStore
    window: OrganizationReportWindow


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated finance-grant PostgreSQL service is required")
    for flag in ("RETAIL_OPERATION_AUDIT_ENABLED", "RETAIL_ORGANIZATIONS_ENABLED", "RETAIL_ORG_FINANCE_ENABLED",
                 "RETAIL_REPORT_EXPORT_ENABLED", "RETAIL_ORGANIZATION_DELEGATION_ENABLED", "RETAIL_ORG_FINANCE_DELEGATION_ENABLED"):
        monkeypatch.setenv(flag, "true")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_finance_grants_"+uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    address: Final = make_conninfo(**{**base, "dbname": name})
    wallet: Final = WalletStore(address, max_connections=2, diagnostic_connections=4, critical_connections=1)
    reports: Final = ReportStore(address, ReportLimits(max_rows=100000, max_days=366), connections=4)
    try:
        async with await psycopg.AsyncConnection.connect(address) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        await wallet.initialize()
        await wallet.initialize_control_plane()
        await reports.initialize()
        await wallet.get_wallet(CUSTOMER, create=True)
        await wallet.credit(CUSTOMER, "1000", "seed", "fixture", "fixture", "seed")
        async with wallet.diagnostic_transaction() as connection:
            await connection.execute("INSERT INTO retail_organizations(id,name) VALUES('org-a','Organization A'),('org-b','Organization B')")
            await connection.execute("""INSERT INTO retail_organization_memberships(organization_id,user_id,role,approved_by)
                VALUES('org-a',%s,'admin','root'),('org-a','root','admin','root'),('org-a',%s,'member','root'),
                ('org-b',%s,'admin','root')""", (DELEGATE, CUSTOMER, DELEGATE))
            await connection.execute("""INSERT INTO retail_billing_organization_assignments
                (user_id,organization_id,created_by,updated_by) VALUES(%s,'org-a','root','root')""", (CUSTOMER,))
        now: Final = datetime.now(timezone.utc)
        yield Runtime(wallet, reports, FinanceGrantStore(wallet), DelegatedReportStore(reports), OrganizationStore(wallet),
                      OrganizationReportWindow(start_at=now-timedelta(days=1), end_at=now+timedelta(minutes=10)))
    finally:
        await reports.close()
        await wallet.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def grant(runtime: Runtime, *, view: bool = True, export: bool = True, user: str = DELEGATE) -> FinanceGrantView:
    current: Final = await runtime.grants.get("org-a", user, ROOT)
    return await runtime.grants.update("org-a", user, FinanceGrantUpdate(can_view=view, can_export=export, expected_version=current.version),
                                       ROOT, "grant-"+uuid4().hex)


async def reserve(runtime: Runtime, request_id: str = "private-request") -> None:
    await runtime.wallet.reserve(CUSTOMER, request_id, "public-model", "0.4", {}, max_parallel=100)


async def member(runtime: Runtime, *, enabled: bool, role: str = "admin") -> None:
    organization: Final = OrganizationRow.model_validate(await runtime.organizations.get("org-a", ROOT))
    async with runtime.base.transaction() as connection:
        current: Final = MembershipRow.model_validate(await (await connection.execute(
            "SELECT * FROM retail_organization_memberships WHERE organization_id='org-a' AND user_id=%s", (DELEGATE,),
        )).fetchone())
    await runtime.organizations.update_member("org-a", DELEGATE, MemberUpdate.model_validate({
        "role": role, "enabled": enabled, "expected_version": current.version, "expected_organization_version": organization.version,
    }), ROOT, "member-"+uuid4().hex)


async def organization(runtime: Runtime, *, enabled: bool, name: str = "Organization A") -> None:
    current: Final = OrganizationRow.model_validate(await runtime.organizations.get("org-a", ROOT))
    await runtime.organizations.update("org-a", OrganizationUpdate(name=name, enabled=enabled, expected_version=current.version),
                                       ROOT, "organization-"+uuid4().hex)


async def account_report(runtime: Runtime, key: str, creator: str = "root") -> None:
    await runtime.base.create(creator, CUSTOMER, key, ReportWindow.model_validate(runtime.window.model_dump()))


@pytest.mark.asyncio
async def test_org_admin_membership_never_implies_finance_and_only_writable_proxy_admin_grants(runtime: Runtime) -> None:
    access: Final = await runtime.grants.access("org-a", DELEGATE)
    assert not access.can_view and not access.can_export and access.access_revision is None
    assert (await runtime.grants.scopes(DELEGATE)).total == 0
    async with runtime.base.transaction() as connection:
        with pytest.raises(HTTPException) as denied:
            await require_finance(connection, "org-a", DELEGATE)
        assert denied.value.status_code == 403
    for actor in (ControlPlaneActor(DELEGATE, "internal_user", False, True), ControlPlaneActor("root", "proxy_admin_viewer", False, False)):
        with pytest.raises(HTTPException) as denied:
            await runtime.grants.update("org-a", DELEGATE, FinanceGrantUpdate(can_view=True, can_export=True, expected_version=0), actor, "denied")
        assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as invalid_target:
        await grant(runtime, user=CUSTOMER)
    assert invalid_target.value.status_code == 422
    saved: Final = await grant(runtime, export=False)
    assert saved.effective and saved.state == "active"
    async with runtime.base.transaction() as connection:
        assert (await require_finance(connection, "org-a", DELEGATE)).can_export is False
        with pytest.raises(HTTPException) as denied_export:
            await require_finance(connection, "org-a", DELEGATE, export=True)
        assert denied_export.value.status_code == 403


@pytest.mark.parametrize("value", ("false", "true", 0, 1))
@pytest.mark.asyncio
async def test_non_boolean_grant_inputs_cannot_change_permissions_or_audits(runtime: Runtime, value: object) -> None:
    with pytest.raises(ValidationError):
        FinanceGrantUpdate.model_validate({"can_view": value, "can_export": False, "expected_version": 0})
    assert (await runtime.grants.get("org-a", DELEGATE, ROOT)).version == 0
    async with runtime.base.transaction() as connection:
        count: Final = await (await connection.execute("SELECT count(*) AS total FROM retail_operation_events WHERE action='finance_grant.updated'")).fetchone()
    assert count == {"total": 0}


@pytest.mark.asyncio
async def test_grants_have_atomic_cas_idempotency_private_audit_and_fresh_revisions(runtime: Runtime) -> None:
    data: Final = FinanceGrantUpdate(can_view=True, can_export=True, expected_version=0)
    first, duplicate = await asyncio.gather(*(runtime.grants.update("org-a", DELEGATE, data, ROOT, "same") for _ in range(2)))
    assert first.grant_id == duplicate.grant_id and first.version == duplicate.version == 1 and first.revision == duplicate.revision
    with pytest.raises(HTTPException) as cas:
        await runtime.grants.update("org-a", DELEGATE, data, ROOT, "stale-cas")
    assert cas.value.status_code == 409
    revoked: Final = await grant(runtime, view=False, export=False)
    renewed: Final = await grant(runtime)
    assert renewed.grant_id == first.grant_id and renewed.version == revoked.version+1 == first.version+2
    assert len({first.revision, revoked.revision, renewed.revision}) == 3
    with pytest.raises(HTTPException) as stale_key:
        await runtime.grants.update("org-a", DELEGATE, data, ROOT, "same")
    assert stale_key.value.status_code == 409
    async with runtime.base.transaction() as connection:
        events: Final = await (await connection.execute("SELECT organization_id,version_after FROM retail_operation_events WHERE action='finance_grant.updated' ORDER BY created_at")).fetchall()
    assert events == [{"organization_id": None, "version_after": version} for version in (1, 2, 3)]


@pytest.mark.asyncio
async def test_finance_generation_changes_only_for_real_org_enabled_changes(runtime: Runtime) -> None:
    await grant(runtime)
    await organization(runtime, enabled=True, name="Renamed")
    async with runtime.base.transaction() as connection:
        original: Final = await require_finance(connection, "org-a", DELEGATE)
        await connection.execute("UPDATE retail_organization_memberships SET version=version+1 WHERE organization_id='org-a' AND user_id='root'")
        await connection.execute("UPDATE retail_organizations SET version=version+1 WHERE id='org-a'")
        after: Final = await require_finance(connection, "org-a", DELEGATE)
    assert original.same_epoch(after) and after.org_generation == 0 and after.organization_name == "Renamed"
    assert (await runtime.grants.scopes(DELEGATE, ids=("org-a", "org-b"))).total == 1
    assert (await runtime.grants.scopes(DELEGATE, q="Renamed", ids=("org-b",))).total == 0


@pytest.mark.parametrize("change", ("member_disabled", "member_demoted", "organization_disabled"))
@pytest.mark.asyncio
async def test_eligibility_break_then_restore_requires_explicit_reapproval_and_never_revives_old_jobs(runtime: Runtime, change: str) -> None:
    await grant(runtime)
    job: Final = await runtime.reports.create(DELEGATE, "org-a", "old-job", runtime.window)
    if change == "organization_disabled":
        await organization(runtime, enabled=False)
    else:
        await member(runtime, enabled=change != "member_disabled", role="member" if change == "member_demoted" else "admin")
    async with runtime.base.transaction() as connection:
        with pytest.raises(HTTPException):
            await require_finance(connection, "org-a", DELEGATE)
    if change == "organization_disabled":
        await organization(runtime, enabled=True)
    else:
        await member(runtime, enabled=True)
    assert not (await runtime.grants.access("org-a", DELEGATE)).can_view
    assert (await runtime.grants.scopes(DELEGATE)).total == 0
    async with runtime.base.transaction() as connection:
        with pytest.raises(HTTPException) as still_denied:
            await require_finance(connection, "org-a", DELEGATE)
        assert still_denied.value.status_code == 403
    renewed: Final = await grant(runtime)
    assert renewed.effective
    with pytest.raises(ReportError, match="report_not_found"):
        await runtime.reports.get(job.id, DELEGATE, "org-a")
    with pytest.raises(ReportError, match="idempotency_conflict"):
        await runtime.reports.create(DELEGATE, "org-a", "old-job", runtime.window)
    assert (await runtime.reports.list(DELEGATE, "org-a")).total == 0


@pytest.mark.asyncio
async def test_revoke_regrant_cannot_resurrect_cached_job_range_or_replay_old_key(runtime: Runtime) -> None:
    await reserve(runtime)
    await runtime.wallet.settle("private-request", "0.25", {})
    await grant(runtime)
    job: Final = await runtime.reports.create(DELEGATE, "org-a", "original", runtime.window)
    claim: Final = await runtime.reports.claim("worker")
    assert claim is not None and await runtime.reports.build(claim)
    cached: Final = await runtime.reports.get(job.id, DELEGATE, "org-a")
    await grant(runtime, view=False, export=False)
    with pytest.raises(HTTPException):
        await runtime.reports.artifact(cached, (0, 3))
    await grant(runtime)
    with pytest.raises(HTTPException) as old_range:
        await runtime.reports.artifact(cached, (0, 3))
    assert old_range.value.status_code == 404
    with pytest.raises(ReportError, match="report_not_found"):
        await runtime.reports.get(job.id, DELEGATE, "org-a")
    with pytest.raises(ReportError, match="idempotency_conflict"):
        await runtime.reports.create(DELEGATE, "org-a", "original", runtime.window)
    assert (await runtime.reports.create(DELEGATE, "org-a", "new-key", runtime.window)).access_revision != job.access_revision


@pytest.mark.asyncio
async def test_safe_fixed_projection_includes_granted_historical_attribution_without_identity_or_errors(runtime: Runtime) -> None:
    await reserve(runtime)
    await grant(runtime)
    job: Final = await runtime.reports.create(DELEGATE, "org-a", "historical", runtime.window)
    await runtime.wallet.settle("private-request", "0.25", {})
    claim: Final = await runtime.reports.claim("worker")
    assert claim is not None and await runtime.reports.build(claim)
    async with runtime.base.transaction() as connection:
        await connection.execute("UPDATE retail_delegated_report_jobs SET error_code='raw-internal-error-marker' WHERE id=%s", (job.id,))
    saved: Final = await runtime.reports.get(job.id, DELEGATE, "org-a")
    content: Final = await runtime.reports.artifact(saved, None)
    exported: Final = next(csv.DictReader(io.StringIO(content.decode())))
    assert exported["state"] == "pending" and exported["settled_nanos"] == exported["settled_usd"] == ""
    assert set(exported) == {"sequence", "model", "captured_at_utc", "captured_at_beijing", "state", "settled_nanos", "settled_usd"}
    public: Final = saved.public().model_dump_json()
    assert all(marker not in public+content.decode() for marker in (
        CUSTOMER, DELEGATE, "private-request", "grant_version", "grant_id", "member_version", "assignment_version",
        "raw-internal-error-marker", "error_code", "provider_cost", "request_id", "user_id",
    ))
    assert saved.public().access_revision == job.access_revision
    assert await runtime.reports.artifact(saved, (0, 10)) == content[:11]
    newer: Final = await runtime.reports.create(DELEGATE, "org-a", "newer", runtime.window)
    assert newer.summary.settled_nanos == "250000000" and job.summary.pending_requests == 1


@pytest.mark.asyncio
async def test_only_global_admin_members_projection_contains_record_existence_even_if_revoked(runtime: Runtime) -> None:
    await grant(runtime, view=False, export=False)
    global_result: Final = await runtime.organizations.members("org-a", ROOT)
    global_members: Final = _ITEMS.validate_python(global_result["items"])
    assert next(row for row in global_members if row["user_id"] == DELEGATE)["has_finance_grant"] is True
    own: Final = ControlPlaneActor(DELEGATE, "internal_user", False, True)
    own_result: Final = await runtime.organizations.members("org-a", own)
    assert all("has_finance_grant" not in row for row in _ITEMS.validate_python(own_result["items"]))


@pytest.mark.asyncio
async def test_grant_audit_failure_rolls_back_configuration_and_creates_no_default_rows(runtime: Runtime) -> None:
    async with runtime.base.transaction() as connection:
        await connection.execute("""CREATE FUNCTION fail_finance_event() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.action='finance_grant.updated' THEN RAISE EXCEPTION 'fixture audit outage'; END IF;
            RETURN NEW; END; $$; CREATE TRIGGER fail_finance_event BEFORE INSERT ON retail_operation_events
            FOR EACH ROW EXECUTE FUNCTION fail_finance_event()""", prepare=False)
    with pytest.raises(psycopg.errors.RaiseException, match="fixture audit outage"):
        await grant(runtime)
    assert (await runtime.grants.get("org-a", DELEGATE, ROOT)).version == 0
    async with runtime.base.transaction() as connection:
        count: Final = await (await connection.execute("SELECT count(*) AS total FROM retail_finance_organization_generations")).fetchone()
    assert count == {"total": 0}


@pytest.mark.asyncio
async def test_grant_and_existing_member_mutation_share_org_first_lock_order(runtime: Runtime) -> None:
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute("SELECT id FROM retail_organizations WHERE id='org-a' FOR UPDATE")
        task: Final = asyncio.create_task(grant(runtime))
        async with asyncio.timeout(2):
            while True:
                await connection.execute("SELECT pg_stat_clear_snapshot()")
                waiting: Final = await (await connection.execute("""SELECT count(*) AS total FROM pg_stat_activity
                    WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event_type='Lock'
                    AND query LIKE '%%retail_organizations%%'""")).fetchone()
                if waiting is not None and waiting["total"]:
                    break
                await asyncio.sleep(0.01)
        await connection.execute("UPDATE retail_organizations SET enabled=false WHERE id='org-a'")
    with pytest.raises(HTTPException) as denied:
        await task
    assert denied.value.status_code == 422 and (await runtime.grants.get("org-a", DELEGATE, ROOT)).version == 0


@pytest.mark.asyncio
async def test_grant_after_waiting_for_org_lock_reads_the_committed_generation(runtime: Runtime) -> None:
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute("SELECT id FROM retail_organizations WHERE id='org-a' FOR UPDATE")
        task: Final = asyncio.create_task(grant(runtime))
        async with asyncio.timeout(2):
            while True:
                await connection.execute("SELECT pg_stat_clear_snapshot()")
                waiting: Final = await (await connection.execute("""SELECT count(*) AS total FROM pg_stat_activity
                    WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event_type='Lock'
                    AND query LIKE '%%retail_organizations%%'""")).fetchone()
                if waiting is not None and waiting["total"]:
                    break
                await asyncio.sleep(0.01)
        await connection.execute("UPDATE retail_organizations SET enabled=false WHERE id='org-a'")
        await connection.execute("UPDATE retail_organizations SET enabled=true WHERE id='org-a'")
    assert (await task).effective
    async with runtime.base.transaction() as connection:
        assert (await require_finance(connection, "org-a", DELEGATE)).org_generation == 2


@pytest.mark.asyncio
async def test_grant_can_be_cleared_after_member_is_disabled(runtime: Runtime) -> None:
    await grant(runtime)
    await member(runtime, enabled=False)
    cleared: Final = await grant(runtime, view=False, export=False)
    assert cleared.state == "revoked" and cleared.version == 2 and not cleared.can_view


@pytest.mark.asyncio
async def test_delegated_snapshots_share_existing_two_slots(runtime: Runtime) -> None:
    await grant(runtime)
    async with runtime.base.transaction() as first, runtime.base.transaction() as second:
        await runtime.base.snapshot_admission(first, "first")
        await runtime.base.snapshot_admission(second, "second")
        with pytest.raises(ReportError, match="report_busy"):
            await runtime.reports.create(DELEGATE, "org-a", "overflow", runtime.window)


@pytest.mark.asyncio
async def test_all_three_queues_share_active_slots_and_round_robin_workers(runtime: Runtime) -> None:
    await grant(runtime, user="root")
    await account_report(runtime, "account")
    await OrganizationReportStore(runtime.base).create("root", "org-a", "admin", runtime.window)
    delegated: Final = await runtime.reports.create("root", "org-a", "delegated", runtime.window)
    first: Final = await runtime.base.claim("first")
    second: Final = await OrganizationReportStore(runtime.base).claim("second")
    assert first is not None and second is not None and await runtime.reports.claim("overflow") is None
    assert await runtime.base.build(first) and await OrganizationReportStore(runtime.base).build(second)
    worker: Final = ReportWorker(runtime.base)
    assert worker.delegated_store.base is runtime.base and await worker.run_once() == 1
    assert (await runtime.reports.get(delegated.id, "root", "org-a")).state == "ready"
    for index in range(2):
        await account_report(runtime, "account-"+str(index))
        await OrganizationReportStore(runtime.base).create("root", "org-a", "admin-"+str(index), runtime.window)
        await runtime.reports.create("root", "org-a", "delegated-"+str(index), runtime.window)
    for _ in range(3):
        assert await worker.run_once() == 1
    async with runtime.base.transaction() as connection:
        ready: Final = await (await connection.execute("""SELECT
            (SELECT count(*) FROM retail_report_jobs WHERE state='ready') AS account,
            (SELECT count(*) FROM retail_organization_report_jobs WHERE state='ready') AS admin,
            (SELECT count(*) FROM retail_delegated_report_jobs WHERE state='ready') AS delegated""")).fetchone()
    assert ready == {"account": 2, "admin": 2, "delegated": 2}


@pytest.mark.asyncio
async def test_three_queue_daily_actor_quota_is_twenty_in_total(runtime: Runtime) -> None:
    await grant(runtime, user="root")
    for index in range(6):
        await account_report(runtime, "account-"+str(index))
        await OrganizationReportStore(runtime.base).create("root", "org-a", "admin-"+str(index), runtime.window)
        await runtime.reports.create("root", "org-a", "delegated-"+str(index), runtime.window)
    await account_report(runtime, "nineteen")
    await account_report(runtime, "twenty")
    for pending in (runtime.reports.create("root", "org-a", "overflow", runtime.window),
                    OrganizationReportStore(runtime.base).create("root", "org-a", "overflow", runtime.window),
                    account_report(runtime, "overflow")):
        with pytest.raises(ReportError, match="report_quota_reached"):
            await pending


@pytest.mark.asyncio
async def test_three_queue_pending_limit_counts_delegated_jobs(runtime: Runtime) -> None:
    await grant(runtime, user="root")
    await account_report(runtime, "account")
    await OrganizationReportStore(runtime.base).create("root", "org-a", "admin", runtime.window)
    await runtime.reports.create("root", "org-a", "delegated", runtime.window)
    async with runtime.base.transaction() as connection:
        await connection.execute("""INSERT INTO retail_report_jobs
            (id,creator_id,account_id,operation_key,request_digest,start_at,end_at,snapshot_at,partial,expires_at,summary,unresolved)
            SELECT gen_random_uuid(),'account-fixture-'||g,account_id,'fixture','digest',start_at,end_at,snapshot_at,
            partial,expires_at,summary,unresolved FROM retail_report_jobs CROSS JOIN generate_series(1,33) g""")
        await connection.execute("""INSERT INTO retail_organization_report_jobs
            (id,creator_id,organization_id,organization_name,operation_key,request_digest,start_at,end_at,snapshot_at,partial,expires_at,summary)
            SELECT gen_random_uuid(),'admin-fixture-'||g,organization_id,organization_name,'fixture','digest',start_at,end_at,snapshot_at,
            partial,expires_at,summary FROM retail_organization_report_jobs CROSS JOIN generate_series(1,32) g""")
        await connection.execute("""INSERT INTO retail_delegated_report_jobs
            (id,creator_id,organization_id,organization_name,grant_id,grant_version,grant_revision,grant_member_version,
             grant_org_generation,operation_key,request_digest,start_at,end_at,snapshot_at,partial,expires_at,summary)
            SELECT gen_random_uuid(),'delegate-fixture-'||g,organization_id,organization_name,grant_id,grant_version,
            grant_revision,grant_member_version,grant_org_generation,'fixture','digest',start_at,end_at,snapshot_at,
            partial,expires_at,summary FROM retail_delegated_report_jobs CROSS JOIN generate_series(1,32) g""")
    with pytest.raises(ReportError, match="report_busy"):
        await runtime.reports.create("root", "org-a", "overflow", runtime.window)
    with pytest.raises(ReportError, match="report_busy"):
        await account_report(runtime, "overflow")


@pytest.mark.asyncio
async def test_flags_pause_delegated_worker_without_adding_pools_or_resuming_old_epoch(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    await grant(runtime)
    job: Final = await runtime.reports.create(DELEGATE, "org-a", "pause", runtime.window)
    worker: Final = ReportWorker(runtime.base)
    monkeypatch.setenv("RETAIL_ORG_FINANCE_DELEGATION_ENABLED", "false")
    assert await worker.run_once() == 0
    assert (await runtime.reports.get(job.id, DELEGATE, "org-a")).state == "queued"
    monkeypatch.setenv("RETAIL_ORG_FINANCE_DELEGATION_ENABLED", "true")
    assert await worker.run_once() == 1
    assert (await runtime.reports.get(job.id, DELEGATE, "org-a")).state == "ready"
