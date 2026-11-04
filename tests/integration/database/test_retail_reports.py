import asyncio
import csv
import hashlib
import io
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail import api, reports
from litellm.proxy.retail.report_store import ReportError, ReportLimits, ReportStore, ReportWindow, get_report_store
from litellm.proxy.retail.report_worker import ReportWorker
from litellm.proxy.retail.store import WalletStore


@dataclass(frozen=True, slots=True)
class NativeWriter:
    database_url: str

    async def query_raw(self, query: str, *args: object) -> object:
        async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as connection:
            return await (await connection.execute(query.replace("$1", "%s"), args)).fetchall()


@dataclass(frozen=True, slots=True)
class NativeClient:
    writer_db: NativeWriter


@dataclass(frozen=True, slots=True)
class ReportRuntime:
    wallet: WalletStore
    reports: ReportStore
    native_url: str
    client: httpx.AsyncClient
    window: ReportWindow


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ReportRuntime]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated report-test PostgreSQL service is required")
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_REPORT_EXPORT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORGANIZATIONS_ENABLED", "false")
    base: Final = conninfo_to_dict(database_url)
    names: Final = ("retail_report_" + uuid4().hex, "retail_report_native_" + uuid4().hex)
    with psycopg.connect(**base, autocommit=True) as connection:
        for name in names:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    retail_url: Final = make_conninfo(**{**base, "dbname": names[0]})
    native_url: Final = make_conninfo(**{**base, "dbname": names[1]})
    wallet: Final = WalletStore(retail_url, max_connections=2, diagnostic_connections=1, critical_connections=1)
    storage: Final = get_report_store(wallet)
    auths: Final = {
        user_id: UserAPIKeyAuth(user_id=user_id, api_key="fixture-" + user_id, user_role=role)
        for user_id, role in (
            ("owner", LitellmUserRoles.INTERNAL_USER),
            ("other", LitellmUserRoles.INTERNAL_USER),
            ("admin", LitellmUserRoles.PROXY_ADMIN),
        )
    }

    async def auth(request: Request) -> UserAPIKeyAuth:
        selected: Final = auths.get(request.headers.get("X-Fixture-Actor", ""))
        if selected is None:
            raise HTTPException(401, "An account credential is required")
        return selected

    async def retail_storage() -> WalletStore:
        return wallet

    app: Final = FastAPI()
    app.include_router(reports.router)
    app.dependency_overrides[api.retail_user] = auth
    app.dependency_overrides[api.retail_store] = retail_storage
    try:
        async with await psycopg.AsyncConnection.connect(retail_url) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        async with await psycopg.AsyncConnection.connect(native_url) as connection:
            await connection.execute("""CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY,user_role TEXT,
                password_reset_required BOOLEAN DEFAULT false)""")
            await connection.execute("""CREATE TABLE "LiteLLM_VerificationToken"(token TEXT PRIMARY KEY,user_id TEXT,
                blocked BOOLEAN DEFAULT false,expires TIMESTAMPTZ,team_id TEXT,allowed_routes TEXT[] DEFAULT '{}')""")
            for user_id, auth_value in auths.items():
                await connection.execute(
                    'INSERT INTO "LiteLLM_UserTable"(user_id,user_role) VALUES(%s,%s)',
                    (user_id, auth_value.user_role.value),
                )
                await connection.execute(
                    'INSERT INTO "LiteLLM_VerificationToken"(token,user_id) VALUES(%s,%s)',
                    (auth_value.api_key, user_id),
                )
        from litellm.proxy import proxy_server

        monkeypatch.setattr(proxy_server, "prisma_client", NativeClient(NativeWriter(native_url)))
        await wallet.initialize()
        await wallet.initialize_control_plane()
        await storage.initialize()
        for user_id in auths:
            await wallet.get_wallet(user_id, create=True)
        async with storage.transaction() as connection:
            row: Final = await (await connection.execute("SELECT now() AS value")).fetchone()
        assert row is not None
        end: Final = (
            row["value"].astimezone(ZoneInfo("Asia/Shanghai")).replace(hour=0, minute=0, second=0, microsecond=0)
        )
        window: Final = ReportWindow(start_at=end - timedelta(days=1), end_at=end)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            yield ReportRuntime(wallet, storage, native_url, client, window)
    finally:
        await storage.close()
        await wallet.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            for name in names:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def seed(runtime: ReportRuntime, count: int = 36, model: str = "public-model") -> None:
    async with runtime.reports.transaction() as connection:
        for index in range(count):
            amount: Final = 9007199254740993 if index == 0 else -index
            await connection.execute(
                """INSERT INTO retail_ledger(id,user_id,kind,amount_nanos,balance_after_nanos,
                operation_key,reference,model,details,payload,created_at) VALUES(%s,'owner',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    uuid4(),
                    "credit" if index == 0 else "charge",
                    amount,
                    amount,
                    "seed-" + str(index),
                    "reference-" + str(index),
                    model,
                    Jsonb({"provider_cost": "private-cost", "api_key": "private-key"}),
                    Jsonb({"prompt": "private-prompt"}),
                    runtime.window.start_at + timedelta(seconds=index),
                ),
            )


async def submit(
    runtime: ReportRuntime,
    actor: str = "owner",
    key: str = "report",
    *,
    kind: Literal["charge"] | None = None,
    account_id: str | None = None,
    organization_id: str | None = None,
) -> httpx.Response:
    return await runtime.client.post(
        "/retail/reports",
        json={
            **runtime.window.model_dump(mode="json"),
            **({"kind": kind} if kind is not None else {}),
            **({"account_id": account_id} if account_id is not None else {}),
            **({"organization_id": organization_id} if organization_id is not None else {}),
        },
        headers={"X-Fixture-Actor": actor, "Idempotency-Key": key},
    )


@pytest.mark.asyncio
async def test_window_including_current_beijing_day_is_explicitly_partial(runtime: ReportRuntime) -> None:
    window: Final = ReportWindow(start_at=runtime.window.start_at, end_at=runtime.window.end_at + timedelta(hours=1))
    created: Final = await runtime.client.post(
        "/retail/reports",
        json=window.model_dump(mode="json"),
        headers={"X-Fixture-Actor": "owner", "Idempotency-Key": "open-day"},
    )
    assert created.status_code == 202, created.text
    assert created.json()["partial"] is True


@pytest.mark.asyncio
async def test_full_window_snapshot_exact_money_idempotency_and_private_range(runtime: ReportRuntime) -> None:
    await seed(runtime)
    created: Final = await submit(runtime)
    assert created.status_code == 202, created.text
    job: Final = created.json()
    assert job["total_rows"] == 36 and job["partial"] is False
    assert job["totals"]["credit_nanos"] == "9007199254740993"
    assert job["totals"]["charge_nanos"] == str(sum(range(1, 36)))
    assert job["totals"]["release_nanos"] == "0"
    assert (await submit(runtime)).json()["id"] == job["id"]
    assert (await submit(runtime, kind="charge")).status_code == 409
    assert await ReportWorker(runtime.reports).run_once() == 1
    path: Final = "/retail/reports/" + job["id"] + "/download"
    downloaded: Final = await runtime.client.get(path, headers={"X-Fixture-Actor": "owner"})
    assert downloaded.status_code == 200, downloaded.text
    records: Final = tuple(csv.DictReader(io.StringIO(downloaded.text)))
    assert len(records) == 36
    assert records[0]["amount_nanos"] == "9007199254740993"
    assert records[1]["amount_nanos"] == "-1"
    assert all(marker not in downloaded.text for marker in ("private-cost", "private-key", "private-prompt"))
    part: Final = await runtime.client.get(path, headers={"X-Fixture-Actor": "owner", "Range": "bytes=7-23"})
    assert part.status_code == 206 and part.content == downloaded.content[7:24]
    assert part.headers["Content-Range"] == f"bytes 7-23/{len(downloaded.content)}"
    assert "private" in part.headers["Cache-Control"] and "no-store" in part.headers["Cache-Control"]
    assert part.headers["Vary"] == "Authorization, Cookie"
    invalid: Final = await runtime.client.get(path, headers={"X-Fixture-Actor": "owner", "Range": "bytes=0-2,4-6"})
    assert invalid.status_code == 416 and invalid.headers["Content-Range"] == f"bytes */{len(downloaded.content)}"
    denied: Final = await runtime.client.get(path)
    assert denied.status_code == 401 and "no-store" in denied.headers["Cache-Control"]
    async with runtime.reports.transaction() as connection:
        audit: Final = await (
            await connection.execute("SELECT action FROM retail_operation_events ORDER BY created_at")
        ).fetchall()
    assert [event["action"] for event in audit] == ["report.created", "report.completed"]


@pytest.mark.asyncio
async def test_snapshot_excludes_uncommitted_rows_and_never_recomputes_old_report(runtime: ReportRuntime) -> None:
    await seed(runtime, count=2)
    async with await psycopg.AsyncConnection.connect(runtime.wallet.database_url) as writer:
        await writer.execute(
            """INSERT INTO retail_ledger(id,user_id,kind,amount_nanos,balance_after_nanos,
            operation_key,created_at) VALUES(%s,'owner','charge',-99,0,'late',%s)""",
            (uuid4(), runtime.window.start_at + timedelta(minutes=1)),
        )
        old: Final = await runtime.reports.create("owner", "owner", "old", runtime.window)
        assert old.total_rows == 2
    new: Final = await runtime.reports.create("owner", "owner", "new", runtime.window)
    assert new.total_rows == 3 and new.totals.charge_nanos == "100"
    await ReportWorker(runtime.reports).run_once()
    await ReportWorker(runtime.reports).run_once()
    stored: Final = await runtime.reports.get(old.id, "owner")
    content: Final = await runtime.reports.artifact(stored, None)
    assert len(tuple(csv.DictReader(io.StringIO(content.decode())))) == 2
    assert stored.summary.charge_nanos == "1"


@pytest.mark.asyncio
async def test_two_workers_recover_abandoned_lease_and_fence_stale_completion(runtime: ReportRuntime) -> None:
    await seed(runtime, count=3)
    jobs: Final = tuple([await runtime.reports.create("owner", "owner", key, runtime.window) for key in ("one", "two")])
    second: Final = ReportStore(runtime.wallet.database_url)
    try:
        abandoned: Final = await runtime.reports.claim("worker-crashed")
        assert abandoned is not None
        assert await ReportWorker(second).run_once() == 1
        async with second.transaction() as connection:
            await connection.execute(
                "UPDATE retail_report_jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (abandoned.id,)
            )
        assert await ReportWorker(second).run_once() == 1
        assert await runtime.reports.build(abandoned) is False
        states: Final = await asyncio.gather(*(runtime.reports.get(job.id, "owner") for job in jobs))
        assert all(job.state == "ready" for job in states)
        async with second.transaction() as connection:
            audit: Final = await (
                await connection.execute(
                    "SELECT count(*) AS total FROM retail_operation_events WHERE action='report.completed'"
                )
            ).fetchone()
        assert audit == {"total": 2}
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_live_account_and_credential_revocation_apply_to_each_download_range(runtime: ReportRuntime) -> None:
    await seed(runtime, count=1)
    created: Final = await submit(runtime)
    assert created.status_code == 202
    report_id: Final = created.json()["id"]
    await ReportWorker(runtime.reports).run_once()
    base: Final = "/retail/reports/" + report_id
    for path in (base, base + "/download"):
        denied: Final = await runtime.client.get(path, headers={"X-Fixture-Actor": "other", "Range": "bytes=0-3"})
        assert denied.status_code == 404 and "no-store" in denied.headers["Cache-Control"]
    assert (await submit(runtime, actor="other", account_id="owner")).status_code == 403
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute("UPDATE \"LiteLLM_VerificationToken\" SET blocked=true WHERE user_id='owner'")
    for path in ("/retail/reports", base, base + "/download"):
        revoked: Final = await runtime.client.get(path, headers={"X-Fixture-Actor": "owner", "Range": "bytes=0-3"})
        assert revoked.status_code == 403 and "no-store" in revoked.headers["Cache-Control"]
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute("UPDATE \"LiteLLM_VerificationToken\" SET blocked=false WHERE user_id='owner'")
        await connection.execute("UPDATE \"LiteLLM_UserTable\" SET password_reset_required=true WHERE user_id='owner'")
    assert (await runtime.client.get(base + "/download", headers={"X-Fixture-Actor": "owner"})).status_code == 403


@pytest.mark.asyncio
async def test_admin_reports_lose_access_after_role_change_and_target_revocation(runtime: ReportRuntime) -> None:
    await seed(runtime, count=1)
    response: Final = await submit(runtime, actor="admin", account_id="owner")
    assert response.status_code == 202, response.text
    report_id: Final = response.json()["id"]
    await ReportWorker(runtime.reports).run_once()
    headers: Final = {"X-Fixture-Actor": "admin"}
    base: Final = "/retail/reports/" + report_id
    assert (await runtime.client.get(base, headers=headers)).status_code == 200
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute("UPDATE \"LiteLLM_UserTable\" SET password_reset_required=true WHERE user_id='owner'")
    assert (await runtime.client.get(base, headers=headers)).status_code == 404
    assert (await runtime.client.get(base + "/download", headers={**headers, "Range": "bytes=0-3"})).status_code == 404
    page: Final = await runtime.client.get("/retail/reports", headers=headers)
    assert page.json()["total"] == 0 and page.json()["items"] == []
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute("UPDATE \"LiteLLM_UserTable\" SET password_reset_required=false WHERE user_id='owner'")
        await connection.execute("UPDATE \"LiteLLM_UserTable\" SET user_role='internal_user' WHERE user_id='admin'")
    assert (await runtime.client.get(base, headers=headers)).status_code == 404
    assert (await runtime.client.get("/retail/reports", headers=headers)).json()["total"] == 0


@pytest.mark.asyncio
async def test_size_rejection_rolls_back_snapshot_and_worker_failure_is_durable(runtime: ReportRuntime) -> None:
    await seed(runtime, count=5, model="m" * 800)
    small_rows: Final = ReportStore(runtime.wallet.database_url, ReportLimits(max_rows=2))
    small_bytes: Final = ReportStore(runtime.wallet.database_url, ReportLimits(max_bytes=1024))
    try:
        with pytest.raises(ReportError, match="row_limit_exceeded"):
            await small_rows.create("owner", "owner", "too-many", runtime.window)
        async with runtime.reports.transaction() as connection:
            count: Final = await (
                await connection.execute(
                    "SELECT (SELECT count(*) FROM retail_report_jobs) AS jobs,(SELECT count(*) FROM retail_report_rows) AS rows"
                )
            ).fetchone()
        assert count == {"jobs": 0, "rows": 0}
        queued: Final = await small_bytes.create("owner", "owner", "too-large", runtime.window)
        assert await ReportWorker(small_bytes).run_once() == 1
        failed: Final = await small_bytes.get(queued.id, "owner")
        assert failed.state == "failed" and failed.error_code == "artifact_too_large" and failed.artifact_bytes == 0
        async with runtime.reports.transaction() as connection:
            event: Final = await (
                await connection.execute(
                    "SELECT count(*) AS total FROM retail_operation_events WHERE action='report.failed'"
                )
            ).fetchone()
        assert event == {"total": 1}
    finally:
        await small_rows.close()
        await small_bytes.close()


@pytest.mark.asyncio
async def test_report_pool_does_not_borrow_finance_or_critical_connections(runtime: ReportRuntime) -> None:
    await seed(runtime, count=1)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(runtime.wallet.transaction())
        await stack.enter_async_context(runtime.wallet.transaction())
        await stack.enter_async_context(runtime.wallet.critical_transaction())
        async with asyncio.timeout(3):
            job: Final = await runtime.reports.create("owner", "owner", "isolated", runtime.window)
            assert await ReportWorker(runtime.reports).run_once() == 1
            assert (await runtime.reports.get(job.id, "owner")).state == "ready"


@pytest.mark.asyncio
async def test_expiry_clears_private_bytes_and_exhausted_worker_is_terminal(runtime: ReportRuntime) -> None:
    await seed(runtime, count=1)
    expired: Final = await runtime.reports.create("owner", "owner", "expire", runtime.window)
    await ReportWorker(runtime.reports).run_once()
    exhausted: Final = await runtime.reports.create("owner", "owner", "attempts", runtime.window)
    claim: Final = await runtime.reports.claim("crash-loop")
    assert claim is not None and claim.id == exhausted.id
    async with runtime.reports.transaction() as connection:
        await connection.execute(
            "UPDATE retail_report_jobs SET expires_at=now()-interval '1 second' WHERE id=%s", (expired.id,)
        )
        await connection.execute(
            "UPDATE retail_report_jobs SET attempts=5,lease_until=now()-interval '1 second' WHERE id=%s",
            (exhausted.id,),
        )
    expired_download: Final = await runtime.client.get(
        f"/retail/reports/{expired.id}/download", headers={"X-Fixture-Actor": "owner"}
    )
    assert expired_download.status_code == 410
    await runtime.reports.expire()
    assert (await runtime.reports.get(exhausted.id, "owner")).state == "failed"
    async with runtime.reports.transaction() as connection:
        cleared: Final = await (
            await connection.execute(
                "SELECT artifact,artifact_bytes,(SELECT count(*) FROM retail_report_rows WHERE report_id=%s) AS rows FROM retail_report_jobs WHERE id=%s",
                (expired.id, expired.id),
            )
        ).fetchone()
    assert cleared == {"artifact": None, "artifact_bytes": 0, "rows": 0}


@pytest.mark.asyncio
async def test_reports_disabled_or_unknown_organization_filters_never_run_export(
    runtime: ReportRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    rejected: Final = await submit(runtime, organization_id="unsupported")
    assert rejected.status_code == 422
    monkeypatch.setenv("RETAIL_REPORT_EXPORT_ENABLED", "false")
    for method, path in (
        ("GET", "/retail/reports"),
        ("POST", "/retail/reports"),
        ("GET", f"/retail/reports/{uuid4()}/download"),
    ):
        result: Final = await runtime.client.request(
            method, path, headers={"X-Fixture-Actor": "owner"}, json=runtime.window.model_dump(mode="json")
        )
        assert result.status_code == 404 and "no-store" in result.headers["Cache-Control"]


@pytest.mark.asyncio
async def test_unresolved_holds_are_separate_from_settled_ledger_and_frozen(runtime: ReportRuntime) -> None:
    await seed(runtime, count=2)
    async with runtime.reports.transaction() as connection:
        for name in ("pending", "unknown", "conflict"):
            await connection.execute(
                """INSERT INTO retail_reservations(request_id,user_id,model,reserved_nanos,
                created_at) VALUES(%s,'owner','public-model',100,%s)""",
                (name, runtime.window.start_at),
            )
            if name != "pending":
                await connection.execute(
                    """INSERT INTO retail_settlement_facts(request_id,details,fact_digest,state)
                    VALUES(%s,'{}',%s,%s)""",
                    (name, "fixture-" + name, name),
                )
    job: Final = await runtime.reports.create("owner", "owner", "unresolved", runtime.window)
    assert job.unresolved.model_dump() == {"pending": 1, "unknown": 1, "conflict": 1}
    assert job.total_rows == 2 and job.totals.charge_nanos == "1"
    async with runtime.reports.transaction() as connection:
        await connection.execute("UPDATE retail_reservations SET status='released'")
        await connection.execute("UPDATE retail_settlement_facts SET state='done'")
    stored: Final = await runtime.reports.get(job.id, "owner")
    assert stored.unresolved == job.unresolved


@pytest.mark.asyncio
async def test_audit_failure_cannot_leave_successful_job_or_partial_snapshot(runtime: ReportRuntime) -> None:
    await seed(runtime, count=2)
    async with runtime.reports.transaction() as connection:
        await connection.execute("""CREATE FUNCTION reject_fixture_audit_write() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Audit writes rejected by fixture'; END; $$""")
        await connection.execute("""CREATE TRIGGER reject_fixture_audit_write BEFORE INSERT ON retail_operation_events
        FOR EACH ROW EXECUTE FUNCTION reject_fixture_audit_write()""")
    with pytest.raises(psycopg.errors.RaiseException, match="Audit writes rejected by fixture"):
        await runtime.reports.create("owner", "owner", "audit-failure", runtime.window)
    async with runtime.reports.transaction() as connection:
        state: Final = await (
            await connection.execute("""SELECT (SELECT count(*) FROM retail_report_jobs) AS jobs,
            (SELECT count(*) FROM retail_report_rows) AS snapshots,(SELECT count(*) FROM retail_ledger) AS ledger""")
        ).fetchone()
    assert state == {"jobs": 0, "snapshots": 0, "ledger": 2}


@pytest.mark.asyncio
async def test_concurrent_duplicate_creates_have_one_snapshot_and_one_creation_audit(runtime: ReportRuntime) -> None:
    await seed(runtime, count=3)
    jobs: Final = await asyncio.gather(
        *(runtime.reports.create("owner", "owner", "same", runtime.window) for _ in range(4))
    )
    assert all(job.id == jobs[0].id for job in jobs)
    async with runtime.reports.transaction() as connection:
        state: Final = await (
            await connection.execute("""SELECT (SELECT count(*) FROM retail_report_jobs) AS jobs,
            (SELECT count(*) FROM retail_report_rows) AS snapshots,(SELECT count(*) FROM retail_operation_events) AS events""")
        ).fetchone()
    assert state == {"jobs": 1, "snapshots": 3, "events": 1}


@pytest.mark.asyncio
async def test_real_csv_validation_rendering_and_hashing_allow_event_loop_progress(runtime: ReportRuntime) -> None:
    rows: Final = tuple(
        {
            "ledger_id": uuid4(),
            "kind": "charge",
            "amount_nanos": -index,
            "balance_after_nanos": index,
            "request_id": None,
            "model": "public-model",
            "created_at": runtime.window.start_at,
        }
        for index in range(10000)
    )
    task: Final = asyncio.create_task(runtime.reports.render(rows))
    ticks = 0  # rebind-ok: count event-loop progress while the render task remains active
    while not task.done():
        ticks += 1
        await asyncio.sleep(0)
    content, digest = await task
    assert ticks > 1
    assert digest == hashlib.sha256(content).hexdigest()
    assert len(tuple(csv.DictReader(io.StringIO(content.decode())))) == len(rows)


@pytest.mark.asyncio
async def test_report_shutdown_closes_dedicated_pool_even_if_lease_release_storage_is_unavailable(
    runtime: ReportRuntime,
) -> None:
    storage: Final = ReportStore(runtime.wallet.database_url)
    await storage.initialize()
    try:
        async with runtime.reports.transaction() as connection:
            await connection.execute("ALTER TABLE retail_report_jobs RENAME TO temporarily_unavailable_report_jobs")
        await ReportWorker(storage).close()
        assert storage.pool.closed
    finally:
        async with runtime.reports.transaction() as connection:
            await connection.execute("ALTER TABLE temporarily_unavailable_report_jobs RENAME TO retail_report_jobs")
        await storage.close()
