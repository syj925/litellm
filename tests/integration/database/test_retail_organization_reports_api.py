import asyncio
import csv
import io
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.retail import api, organization_reports
from litellm.proxy.retail.audit_delivery_store import AuditDeliveryStore
from litellm.proxy.retail.control_plane_access import ControlPlaneActor
from litellm.proxy.retail.organization_report_store import OrganizationReportStore, OrganizationReportWindow
from litellm.proxy.retail.report_store import ReportStore, get_report_store
from litellm.proxy.retail.settlement_store import record_facts
from litellm.proxy.retail.store import WalletStore

ROOT: Final = "/retail/admin/organizations/org-a/reports"


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
class Runtime:
    store: WalletStore
    reports: ReportStore
    organization_reports: OrganizationReportStore
    client: httpx.AsyncClient
    native_url: str
    window: OrganizationReportWindow


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated organization-finance test PostgreSQL service is required")
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORGANIZATIONS_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORG_FINANCE_ENABLED", "true")
    monkeypatch.setenv("RETAIL_REPORT_EXPORT_ENABLED", "true")
    base: Final = conninfo_to_dict(database_url)
    names: Final = ("retail_org_report_api_" + uuid4().hex, "native_org_report_api_" + uuid4().hex)
    with psycopg.connect(**base, autocommit=True) as connection:
        for name in names:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    address: Final = make_conninfo(**{**base, "dbname": names[0]})
    native_url: Final = make_conninfo(**{**base, "dbname": names[1]})
    store: Final = WalletStore(address, max_connections=4, diagnostic_connections=4, critical_connections=2)
    reports_store: Final = get_report_store(store)
    org_reports: Final = OrganizationReportStore(reports_store)
    auths: Final = {
        user_id: UserAPIKeyAuth(
            user_id=user_id, api_key="fixture-" + user_id, user_role=role, team_id=UI_SESSION_TOKEN_TEAM_ID
        )
        for user_id, role in (
            ("owner", LitellmUserRoles.INTERNAL_USER),
            ("other", LitellmUserRoles.INTERNAL_USER),
            ("admin", LitellmUserRoles.PROXY_ADMIN),
            ("other-admin", LitellmUserRoles.PROXY_ADMIN),
            ("viewer", LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY),
            ("org-admin", LitellmUserRoles.INTERNAL_USER),
        )
    }

    async def auth(request: Request) -> UserAPIKeyAuth:
        selected: Final = auths.get(request.headers.get("X-Fixture-Actor", ""))
        if selected is None:
            raise HTTPException(401, "An account credential is required")
        return selected

    async def storage() -> WalletStore:
        return store

    app: Final = FastAPI()
    app.include_router(organization_reports.router)
    app.include_router(api.router)
    app.dependency_overrides[user_api_key_auth] = auth
    app.dependency_overrides[api.retail_store] = storage
    try:
        async with await psycopg.AsyncConnection.connect(address) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        async with await psycopg.AsyncConnection.connect(native_url) as connection:
            await connection.execute("""CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY,user_role TEXT,
                password_reset_required BOOLEAN DEFAULT false)""")
            await connection.execute("""CREATE TABLE "LiteLLM_VerificationToken"(token TEXT PRIMARY KEY,user_id TEXT,
                team_id TEXT,blocked BOOLEAN DEFAULT false,expires TIMESTAMPTZ,allowed_routes TEXT[] DEFAULT '{}')""")
            for user_id, auth_value in auths.items():
                await connection.execute(
                    'INSERT INTO "LiteLLM_UserTable"(user_id,user_role) VALUES(%s,%s)',
                    (user_id, auth_value.user_role.value),
                )
                await connection.execute(
                    'INSERT INTO "LiteLLM_VerificationToken"(token,user_id,team_id) VALUES(%s,%s,%s)',
                    (auth_value.api_key, user_id, UI_SESSION_TOKEN_TEAM_ID),
                )
            await connection.execute(
                "INSERT INTO \"LiteLLM_UserTable\"(user_id,user_role) VALUES('no-wallet','internal_user')"
            )
        from litellm.proxy import proxy_server

        monkeypatch.setattr(proxy_server, "prisma_client", NativeClient(NativeWriter(native_url)))
        await store.initialize()
        await store.initialize_control_plane()
        await reports_store.initialize()
        for user_id in auths:
            await store.get_wallet(user_id, create=True)
            await store.credit(user_id, "100", "seed-" + user_id, "fixture", "fixture", "seed-" + user_id)
        async with store.diagnostic_transaction() as connection:
            await connection.execute("""INSERT INTO retail_organizations(id,name,version)
                VALUES('org-a','Fixture A',2),('org-b','Fixture B',3),('org-disabled','Disabled',1),
                ('org-ineligible','No membership',1)""")
            await connection.execute("UPDATE retail_organizations SET enabled=false WHERE id='org-disabled'")
            await connection.execute("""INSERT INTO retail_organization_memberships
                (organization_id,user_id,role,approved_by,version) VALUES
                ('org-a','owner','member','admin',4),('org-b','owner','member','admin',5),
                ('org-disabled','owner','member','admin',1),('org-a','org-admin','admin','admin',1)""")
            clock: Final = await (await connection.execute("SELECT clock_timestamp() AS at")).fetchone()
        assert clock is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            yield Runtime(store, reports_store, org_reports, client, native_url, OrganizationReportWindow(
                start_at=clock["at"] - timedelta(days=1), end_at=clock["at"] + timedelta(days=1)
            ))
    finally:
        await reports_store.close()
        await store.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            for name in names:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def create(runtime: Runtime, *, key: str = "create", actor: str = "admin", path: str = ROOT) -> httpx.Response:
    return await runtime.client.post(
        path, json=runtime.window.model_dump(mode="json"),
        headers={"X-Fixture-Actor": actor, "Idempotency-Key": key},
    )


async def ready(runtime: Runtime, report_id: str) -> None:
    claim: Final = await runtime.organization_reports.claim("fixture-worker")
    assert claim is not None
    assert str(claim.id) == report_id
    assert await runtime.organization_reports.build(claim) is True


async def seed(runtime: Runtime) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("""INSERT INTO retail_billing_organization_assignments
            (user_id,organization_id,created_by,updated_by) VALUES('owner','org-a','admin','admin')""")
    for request_id in ("paid", "zero", "pending", "released", "unknown"):
        await runtime.store.reserve("owner", request_id, "public-model", "0.4", {}, max_parallel=100)
    await runtime.store.settle("paid", "0.123456789", {"api_key": "private-fixture-key"})
    await runtime.store.settle("zero", "0", {})
    await runtime.store.release("released", "fixture")
    await record_facts(runtime.store, "unknown", None, {"billing_error": "private-fixture-diagnostic"})


def assert_private(response: httpx.Response, status: int) -> None:
    assert response.status_code == status, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Vary"] == "Authorization, Cookie"


@pytest.mark.asyncio
async def test_frozen_report_round_trip_exact_money_pages_ranges_and_late_settlement(runtime: Runtime) -> None:
    await seed(runtime)
    created: Final = await create(runtime)
    assert_private(created, 202)
    job: Final = created.json()
    assert job["scope"] == "organization"
    assert job["axis"] == "admission"
    assert job["organization_id"] == "org-a"
    assert job["total_rows"] == 5
    assert job["summary"]["settled_usd"] == "0.123456789"
    assert job["summary"]["pending_requests"] == job["summary"]["unknown_requests"] == 1
    assert (await create(runtime)).json()["id"] == job["id"]
    path: Final = ROOT + "/" + job["id"]
    assert_private(await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "admin"}), 409)
    await ready(runtime, job["id"])
    downloaded: Final = await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "admin"})
    assert_private(downloaded, 200)
    assert downloaded.headers["Accept-Ranges"] == "bytes"
    assert downloaded.headers["X-Content-Type-Options"] == "nosniff"
    assert "attachment" in downloaded.headers["Content-Disposition"]
    rows: Final = tuple(csv.DictReader(io.StringIO(downloaded.text)))
    assert len(rows) == 5
    assert {row["request_id"] for row in rows} == {"paid", "zero", "pending", "released", "unknown"}
    assert all(value not in downloaded.text for value in ("private-fixture-key", "private-fixture-diagnostic"))
    partial: Final = await runtime.client.get(
        path + "/download", headers={"X-Fixture-Actor": "admin", "Range": "bytes=7-23"},
    )
    assert_private(partial, 206)
    assert partial.content == downloaded.content[7:24]
    assert partial.headers["Content-Range"] == f"bytes 7-23/{len(downloaded.content)}"
    assert partial.headers["ETag"] == downloaded.headers["ETag"]
    invalid: Final = await runtime.client.get(
        path + "/download", headers={"X-Fixture-Actor": "admin", "Range": "bytes=0-2,5-7"},
    )
    assert_private(invalid, 416)
    assert invalid.headers["Content-Range"] == f"bytes */{len(downloaded.content)}"
    await runtime.store.settle("pending", "0.5", {})
    stable: Final = await runtime.client.get(path, headers={"X-Fixture-Actor": "admin"})
    assert stable.json()["summary"] == job["summary"]
    repeated: Final = await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "admin"})
    assert repeated.content == downloaded.content
    latest: Final = await create(runtime, key="after-late-settlement")
    assert_private(latest, 202)
    assert latest.json()["summary"]["settled_usd"] == "0.623456789"
    page: Final = await runtime.client.get(ROOT, params={"limit": 1}, headers={"X-Fixture-Actor": "admin"})
    assert_private(page, 200)
    assert page.json()["total"] == 2
    assert len(page.json()["items"]) == 1
    second: Final = await runtime.client.get(ROOT, params={"limit": 1, "offset": 1},
                                            headers={"X-Fixture-Actor": "admin"})
    assert second.json()["total"] == 2
    assert second.json()["items"][0]["id"] != page.json()["items"][0]["id"]


@pytest.mark.asyncio
async def test_other_admin_and_different_url_organization_cannot_read_or_download_job(runtime: Runtime) -> None:
    created: Final = await create(runtime)
    assert_private(created, 202)
    job: Final = created.json()
    await ready(runtime, job["id"])
    path: Final = ROOT + "/" + job["id"]
    assert_private(await runtime.client.get(path, headers={"X-Fixture-Actor": "other-admin"}), 404)
    assert_private(await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "other-admin"}), 404)
    page: Final = await runtime.client.get(ROOT, headers={"X-Fixture-Actor": "other-admin"})
    assert_private(page, 200)
    assert page.json()["items"] == []
    assert page.json()["total"] == 0
    assert_private(await runtime.client.get(path.replace("org-a", "org-b"),
                                            headers={"X-Fixture-Actor": "admin"}), 404)
    assert_private(await runtime.client.get(path.replace("org-a", "org-b") + "/download",
                                            headers={"X-Fixture-Actor": "admin", "Range": "bytes=0-2"}), 404)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ("owner", "other", "org-admin", "viewer"))
async def test_customer_delegated_and_readonly_actors_have_no_report_access(runtime: Runtime, actor: str) -> None:
    created: Final = await create(runtime)
    job: Final = created.json()
    path: Final = ROOT + "/" + job["id"]
    assert_private(await create(runtime, actor=actor), 403)
    for target in (ROOT, path, path + "/download"):
        assert_private(await runtime.client.get(target, headers={"X-Fixture-Actor": actor}), 403)


@pytest.mark.asyncio
async def test_missing_authentication_is_private_and_cannot_list_jobs(runtime: Runtime) -> None:
    assert_private(await runtime.client.get(ROOT), 401)
    assert_private(await runtime.client.post(ROOT, json=runtime.window.model_dump(mode="json")), 401)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ("role", "viewer-role", "credential", "password"))
async def test_fresh_authority_revocation_blocks_cached_actor_and_idempotent_replay(
    runtime: Runtime, revocation: str,
) -> None:
    created: Final = await create(runtime)
    job: Final = created.json()
    path: Final = ROOT + "/" + job["id"]
    queries: Final = {
        "role": "UPDATE \"LiteLLM_UserTable\" SET user_role='internal_user' WHERE user_id='admin'",
        "viewer-role": "UPDATE \"LiteLLM_UserTable\" SET user_role='proxy_admin_viewer' WHERE user_id='admin'",
        "credential": "UPDATE \"LiteLLM_VerificationToken\" SET blocked=true WHERE user_id='admin'",
        "password": "UPDATE \"LiteLLM_UserTable\" SET password_reset_required=true WHERE user_id='admin'",
    }
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute(queries[revocation])
    await ready(runtime, job["id"])
    assert_private(await create(runtime), 403)
    for target in (ROOT, path, path + "/download"):
        assert_private(await runtime.client.get(target, headers={"X-Fixture-Actor": "admin"}), 403)


@pytest.mark.asyncio
async def test_expired_artifact_cannot_be_read_even_by_its_creator(runtime: Runtime) -> None:
    created: Final = await create(runtime)
    job: Final = created.json()
    await ready(runtime, job["id"])
    path: Final = ROOT + "/" + job["id"]
    async with runtime.reports.transaction() as connection:
        await connection.execute("UPDATE retail_organization_report_jobs SET expires_at=now()-interval '1 second' "
                                 "WHERE id=%s", (job["id"],))
    metadata: Final = await runtime.client.get(path, headers={"X-Fixture-Actor": "admin"})
    assert_private(metadata, 200)
    assert metadata.json()["state"] == "expired"
    assert_private(await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "admin"}), 410)


@pytest.mark.asyncio
async def test_disabled_organization_blocks_all_access_and_reenabling_restores_frozen_job(runtime: Runtime) -> None:
    created: Final = await create(runtime)
    job: Final = created.json()
    await ready(runtime, job["id"])
    path: Final = ROOT + "/" + job["id"]
    before: Final = await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "admin"})
    assert_private(before, 200)
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_organizations SET enabled=false WHERE id='org-a'")
    assert_private(await create(runtime), 404)
    for target in (ROOT, path, path + "/download"):
        assert_private(await runtime.client.get(target, headers={"X-Fixture-Actor": "admin", "Range": "bytes=0-2"}), 404)
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_organizations SET enabled=true WHERE id='org-a'")
    restored: Final = await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "admin"})
    assert_private(restored, 200)
    assert restored.content == before.content
    assert (await create(runtime)).json()["id"] == job["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ("RETAIL_REPORT_EXPORT_ENABLED", "RETAIL_ORG_FINANCE_ENABLED",
                                  "RETAIL_ORGANIZATIONS_ENABLED", "RETAIL_OPERATION_AUDIT_ENABLED"))
async def test_dependency_shutdown_hides_create_list_detail_and_download(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, flag: str,
) -> None:
    created: Final = await create(runtime)
    job: Final = created.json()
    path: Final = ROOT + "/" + job["id"]
    monkeypatch.setenv(flag, "false")
    assert_private(await create(runtime), 404)
    for target in (ROOT, path, path + "/download"):
        assert_private(await runtime.client.get(target, headers={"X-Fixture-Actor": "admin"}), 404)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("account_id", "kind", "organization_id", "user_id", "provider_cost"))
async def test_request_rejects_cross_scope_and_sensitive_fields_without_echo(runtime: Runtime, field: str) -> None:
    response: Final = await runtime.client.post(
        ROOT, json={**runtime.window.model_dump(mode="json"), field: "private-fixture-value"},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "invalid"},
    )
    assert_private(response, 422)
    assert "private-fixture-value" not in response.text


@pytest.mark.asyncio
async def test_unexpected_queries_and_invalid_ids_and_windows_are_private(runtime: Runtime) -> None:
    bad_query: Final = await runtime.client.get(ROOT, params={"account_id": "private-fixture-value"},
                                               headers={"X-Fixture-Actor": "admin"})
    assert_private(bad_query, 422)
    assert "private-fixture-value" not in bad_query.text
    assert_private(await runtime.client.get(ROOT + "/private-fixture-value",
                                            headers={"X-Fixture-Actor": "admin"}), 422)
    bad_time: Final = await runtime.client.post(
        ROOT, json={"start_at": "2025-01-01T00:00:00", "end_at": "2025-01-02T00:00:00"},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "naive-time"},
    )
    assert_private(bad_time, 422)
    oversized: Final = await runtime.client.post(
        ROOT, json={**runtime.window.model_dump(mode="json"),
                    "end_at": (runtime.window.start_at + timedelta(days=32)).isoformat()},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "oversized"},
    )
    assert_private(oversized, 422)
    for target in (ROOT + "?limit=101", ROOT + "?offset=10001"):
        assert_private(await runtime.client.get(target, headers={"X-Fixture-Actor": "admin"}), 422)
    assert_private(await create(runtime, path=ROOT.replace("org-a", "missing")), 404)
    invalid_organization: Final = await create(runtime, path=ROOT.replace("org-a", "%20org-a%20"))
    assert_private(invalid_organization, 422)


@pytest.mark.asyncio
async def test_concurrent_idempotent_submissions_create_one_frozen_job(runtime: Runtime) -> None:
    first, second = await asyncio.gather(create(runtime), create(runtime))
    assert_private(first, 202)
    assert_private(second, 202)
    assert first.json()["id"] == second.json()["id"]
    changed: Final = await runtime.client.post(
        ROOT, json={**runtime.window.model_dump(mode="json"),
                    "start_at": (runtime.window.start_at - timedelta(seconds=1)).isoformat()},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "create"},
    )
    assert_private(changed, 409)
    page: Final = await runtime.client.get(ROOT, headers={"X-Fixture-Actor": "admin"})
    assert page.json()["total"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ("%20private-fixture-value%20", "%00private-fixture-value", "x" * 192))
async def test_encoded_invalid_organization_id_is_rejected_for_every_operation(
    runtime: Runtime, identifier: str,
) -> None:
    root: Final = ROOT.replace("org-a", identifier)
    path: Final = root + "/" + str(uuid4())
    response: Final = await create(runtime, path=root)
    assert_private(response, 422)
    assert "private-fixture-value" not in response.text
    for target in (root, path, path + "/download"):
        result: Final = await runtime.client.get(target, headers={"X-Fixture-Actor": "admin"})
        assert_private(result, 422)
        assert "private-fixture-value" not in result.text


@pytest.mark.asyncio
async def test_creation_audit_failure_rolls_back_job_and_snapshot(runtime: Runtime) -> None:
    await seed(runtime)
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("""CREATE FUNCTION fixture_reject_report_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'private-fixture-error' USING ERRCODE='57014'; END $$""")
        await connection.execute("CREATE TRIGGER fixture_reject_report_audit BEFORE INSERT ON retail_operation_events "
                                 "FOR EACH ROW EXECUTE FUNCTION fixture_reject_report_audit()")
    failed: Final = await create(runtime)
    assert_private(failed, 503)
    assert "private-fixture-error" not in failed.text
    async with runtime.store.diagnostic_transaction() as connection:
        counts: Final = await (await connection.execute("SELECT "
            "(SELECT count(*) FROM retail_organization_report_jobs) AS jobs,"
            "(SELECT count(*) FROM retail_organization_report_rows) AS rows")).fetchone()
    assert counts == {"jobs": 0, "rows": 0}


@pytest.mark.asyncio
async def test_organization_report_audit_delivery_is_seven_field_metadata_only(runtime: Runtime) -> None:
    delivery: Final = AuditDeliveryStore(runtime.store)
    fingerprint: Final = "b" * 64
    try:
        await delivery.initialize()
        await delivery.configure(True, 1, fingerprint, ControlPlaneActor("admin", "proxy_admin", True, True), "enable")
        created: Final = await create(runtime)
        assert_private(created, 202)
        await ready(runtime, created.json()["id"])
        first: Final = await delivery.claim("fixture-first", fingerprint)
        second: Final = await delivery.claim("fixture-second", fingerprint)
        assert first is not None
        assert second is not None
        assert {first.payload.action, second.payload.action} == {"organization_report.created", "organization_report.completed"}
        for claim in (first, second):
            payload: Final = claim.payload.model_dump(mode="json")
            assert set(payload) == {"event_id", "action", "resource_type", "result", "version_before",
                                    "version_after", "occurred_at"}
            assert payload["resource_type"] == "organization_report"
            assert "org-a" not in str(payload)
            assert "admin" not in str(payload)
    finally:
        await delivery.close()
