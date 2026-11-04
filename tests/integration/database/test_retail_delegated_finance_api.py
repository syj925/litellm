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
from litellm.proxy.retail import api, delegated_finance, organization_reports, organizations
from litellm.proxy.retail.audit_delivery_store import AuditDeliveryStore
from litellm.proxy.retail.control_plane_access import ControlPlaneActor
from litellm.proxy.retail.delegated_report_store import DelegatedReportStore
from litellm.proxy.retail.organization_report_store import OrganizationReportWindow
from litellm.proxy.retail.report_store import ReportStore, get_report_store
from litellm.proxy.retail.settlement_store import record_facts
from litellm.proxy.retail.store import WalletStore

ROOT: Final = "/retail/organizations/org-a/finance"
GRANT: Final = "/retail/admin/organizations/org-a/finance-grants/delegate"
SCOPES: Final = "/retail/organization-finance/organizations"


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
    delegated_reports: DelegatedReportStore
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
    monkeypatch.setenv("RETAIL_ORGANIZATION_DELEGATION_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORG_FINANCE_DELEGATION_ENABLED", "true")
    base: Final = conninfo_to_dict(database_url)
    names: Final = ("retail_delegated_api_" + uuid4().hex, "native_delegated_api_" + uuid4().hex)
    with psycopg.connect(**base, autocommit=True) as connection:
        for name in names:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    address: Final = make_conninfo(**{**base, "dbname": names[0]})
    native_url: Final = make_conninfo(**{**base, "dbname": names[1]})
    store: Final = WalletStore(address, max_connections=4, diagnostic_connections=4, critical_connections=2)
    reports_store: Final = get_report_store(store)
    delegated_reports: Final = DelegatedReportStore(reports_store)
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
            ("delegate", LitellmUserRoles.INTERNAL_USER),
            ("delegate2", LitellmUserRoles.INTERNAL_USER),
            ("readonly", LitellmUserRoles.INTERNAL_USER_VIEW_ONLY),
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
    app.include_router(delegated_finance.router)
    app.include_router(organizations.router)
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
                ('org-disabled','owner','member','admin',1),('org-a','org-admin','admin','admin',1),
                ('org-a','delegate','admin','admin',2),('org-b','delegate','admin','admin',3),
                ('org-a','delegate2','admin','admin',4),('org-a','readonly','admin','admin',5)""")
            clock: Final = await (await connection.execute("SELECT clock_timestamp() AS at")).fetchone()
        assert clock is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            yield Runtime(store, reports_store, delegated_reports, client, native_url, OrganizationReportWindow(
                start_at=clock["at"] - timedelta(days=1), end_at=clock["at"] + timedelta(days=1)
            ))
    finally:
        await reports_store.close()
        await store.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            for name in names:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def assert_private(response: httpx.Response, status: int) -> None:
    assert response.status_code == status, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Vary"] == "Authorization, Cookie"


async def grant(
    runtime: Runtime, *, view: bool = True, export: bool = True, version: int = 0,
    key: str = "grant", target: str = "delegate", actor: str = "admin",
) -> httpx.Response:
    return await runtime.client.patch(
        GRANT.replace("/delegate", "/"+target),
        json={"can_view": view, "can_export": export, "expected_version": version},
        headers={"X-Fixture-Actor": actor, "Idempotency-Key": key},
    )


async def create(runtime: Runtime, *, key: str = "create", actor: str = "delegate") -> httpx.Response:
    return await runtime.client.post(
        ROOT + "/reports", json=runtime.window.model_dump(mode="json"),
        headers={"X-Fixture-Actor": actor, "Idempotency-Key": key},
    )


async def ready(runtime: Runtime, report_id: str) -> None:
    claim: Final = await runtime.delegated_reports.claim("fixture-worker")
    assert claim is not None
    assert str(claim.id) == report_id
    assert await runtime.delegated_reports.build(claim) is True


async def seed(runtime: Runtime) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("""INSERT INTO retail_billing_organization_assignments
            (user_id,organization_id,created_by,updated_by) VALUES('owner','org-a','admin','admin')""")
    for request_id in ("private-paid-id", "private-zero-id", "private-pending-id", "private-unknown-id"):
        await runtime.store.reserve("owner", request_id, "public-model", "0.4", {}, max_parallel=100)
    await runtime.store.settle("private-paid-id", "0.123456789", {"provider_cost": "private-fixture-cost"})
    await runtime.store.settle("private-zero-id", "0", {})
    await record_facts(runtime.store, "private-unknown-id", None, {"billing_error": "private-fixture-diagnostic"})


@pytest.mark.asyncio
async def test_explicit_view_grant_exposes_safe_exact_cohort_and_bounded_scope_directory(runtime: Runtime) -> None:
    await seed(runtime)
    before: Final = await runtime.store.get_wallet("owner")
    absent: Final = await runtime.client.get(ROOT + "/access", headers={"X-Fixture-Actor": "delegate"})
    assert_private(absent, 200)
    assert absent.json()["can_view"] is False
    assert_private(await runtime.client.get(ROOT + "/consumption", headers={"X-Fixture-Actor": "delegate"}), 403)
    selected: Final = await grant(runtime, export=False)
    assert_private(selected, 200)
    assert selected.json()["state"] == "active"
    assert selected.json()["version"] == 1
    scoped: Final = await runtime.client.get(SCOPES, headers={"X-Fixture-Actor": "delegate"})
    assert_private(scoped, 200)
    assert scoped.json()["total"] == 1
    item: Final = scoped.json()["items"][0]
    assert set(item) == {"id", "name", "can_view", "can_export", "access_revision"}
    assert item["id"] == "org-a" and item["can_export"] is False
    view: Final = await runtime.client.get(ROOT + "/consumption", params={"limit": 1},
                                          headers={"X-Fixture-Actor": "delegate"})
    assert_private(view, 200)
    body: Final = view.json()
    assert body["total"] == 4
    assert body["summary"]["settled_usd"] == "0.123456789"
    assert body["summary"]["pending_requests"] == body["summary"]["unknown_requests"] == 1
    assert body["access_revision"] == item["access_revision"]
    assert len(body["items"]) == 1
    assert set(body["items"][0]) == {"sequence", "model", "captured_at", "state", "settled_usd"}
    page: Final = await runtime.client.get(ROOT + "/consumption", params={"limit": 1, "offset": 3},
                                          headers={"X-Fixture-Actor": "delegate"})
    assert page.json()["summary"] == body["summary"]
    assert page.json()["items"][0]["sequence"] != body["items"][0]["sequence"]
    assert "private-" not in view.text and '"owner"' not in view.text
    assert_private(await create(runtime), 403)
    assert_private(await runtime.client.get(ROOT + "/reports", headers={"X-Fixture-Actor": "delegate"}), 403)
    assert await runtime.store.get_wallet("owner") == before


@pytest.mark.asyncio
async def test_export_permission_creates_private_safe_csv_and_supports_authenticated_ranges(runtime: Runtime) -> None:
    await seed(runtime)
    assert_private(await grant(runtime), 200)
    created: Final = await create(runtime)
    assert_private(created, 202)
    job: Final = created.json()
    assert job["scope"] == "delegated_organization"
    assert "error_code" not in job
    assert job["summary"]["settled_usd"] == "0.123456789"
    assert (await create(runtime)).json()["id"] == job["id"]
    path: Final = ROOT + "/reports/" + job["id"]
    assert_private(await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "delegate"}), 409)
    await ready(runtime, job["id"])
    download: Final = await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "delegate"})
    assert_private(download, 200)
    rows: Final = tuple(csv.DictReader(io.StringIO(download.text)))
    assert len(rows) == 4
    assert set(rows[0]) == {"sequence", "model", "captured_at_utc", "captured_at_beijing", "state",
                           "settled_nanos", "settled_usd"}
    assert "private-" not in download.text and "owner" not in download.text
    assert {row["settled_usd"] for row in rows} == {"0.123456789", "0.000000000", ""}
    partial: Final = await runtime.client.get(path + "/download",
        headers={"X-Fixture-Actor": "delegate", "Range": "bytes=7-23"})
    assert_private(partial, 206)
    assert partial.content == download.content[7:24]
    assert partial.headers["ETag"] == download.headers["ETag"]
    assert partial.headers["Content-Range"] == f"bytes 7-23/{len(download.content)}"
    invalid: Final = await runtime.client.get(path + "/download",
        headers={"X-Fixture-Actor": "delegate", "Range": "bytes=0-1,3-4"})
    assert_private(invalid, 416)
    await runtime.store.settle("private-pending-id", "0.5", {})
    repeated: Final = await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "delegate"})
    assert repeated.content == download.content
    assert_private(await runtime.client.get(path + "/download"), 401)


@pytest.mark.asyncio
async def test_revoke_reapprove_changes_epoch_and_never_restores_old_artifact_or_receipt(runtime: Runtime) -> None:
    first: Final = await grant(runtime)
    assert_private(first, 200)
    created: Final = await create(runtime)
    job: Final = created.json()
    await ready(runtime, job["id"])
    path: Final = ROOT + "/reports/" + job["id"]
    revoked: Final = await grant(runtime, view=False, export=False, version=1, key="revoke")
    assert_private(revoked, 200)
    assert_private(await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "delegate"}), 403)
    second: Final = await grant(runtime, version=2, key="reapprove")
    assert_private(second, 200)
    assert second.json()["revision"] != first.json()["revision"]
    for target in (path, path + "/download"):
        assert_private(await runtime.client.get(target, headers={"X-Fixture-Actor": "delegate"}), 404)
    listed: Final = await runtime.client.get(ROOT + "/reports", headers={"X-Fixture-Actor": "delegate"})
    assert listed.json()["total"] == 0 and listed.json()["items"] == []
    assert_private(await create(runtime), 409)
    fresh: Final = await create(runtime, key="new-epoch")
    assert_private(fresh, 202)
    assert fresh.json()["id"] != job["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("membership", "organization"))
async def test_stop_restore_requires_explicit_reapproval_and_fences_old_reports(runtime: Runtime, change: str) -> None:
    assert_private(await grant(runtime), 200)
    created: Final = await create(runtime)
    job: Final = created.json()
    await ready(runtime, job["id"])
    async with runtime.store.diagnostic_transaction() as connection:
        if change == "membership":
            await connection.execute("UPDATE retail_organization_memberships SET enabled=false,version=version+1 "
                                     "WHERE organization_id='org-a' AND user_id='delegate'")
            await connection.execute("UPDATE retail_organization_memberships SET enabled=true,version=version+1 "
                                     "WHERE organization_id='org-a' AND user_id='delegate'")
        else:
            await connection.execute("UPDATE retail_organizations SET enabled=false WHERE id='org-a'")
            await connection.execute("UPDATE retail_organizations SET enabled=true WHERE id='org-a'")
    access: Final = await runtime.client.get(ROOT + "/access", headers={"X-Fixture-Actor": "delegate"})
    assert access.json()["can_view"] is False
    assert_private(await runtime.client.get(ROOT + "/consumption", headers={"X-Fixture-Actor": "delegate"}), 403)
    assert_private(await grant(runtime, version=1, key="reapprove"), 200)
    assert_private(await runtime.client.get(ROOT + "/reports/" + job["id"] + "/download",
                                           headers={"X-Fixture-Actor": "delegate"}), 404)


@pytest.mark.asyncio
async def test_organization_rename_preserves_access_revision_and_existing_report(runtime: Runtime) -> None:
    original: Final = await grant(runtime)
    created: Final = await create(runtime)
    job: Final = created.json()
    await ready(runtime, job["id"])
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_organizations SET name='New name',version=version+1 WHERE id='org-a'")
    access: Final = await runtime.client.get(ROOT + "/access", headers={"X-Fixture-Actor": "delegate"})
    assert access.json()["access_revision"] == original.json()["revision"]
    assert_private(await runtime.client.get(ROOT + "/reports/" + job["id"] + "/download",
                                           headers={"X-Fixture-Actor": "delegate"}), 200)


@pytest.mark.asyncio
async def test_current_grants_never_share_creator_or_organization_report_namespaces(runtime: Runtime) -> None:
    assert_private(await grant(runtime), 200)
    assert_private(await grant(runtime, target="delegate2", key="grant-second"), 200)
    created: Final = await create(runtime)
    job: Final = created.json()
    await ready(runtime, job["id"])
    path: Final = ROOT + "/reports/" + job["id"]
    for target in (path, path + "/download"):
        assert_private(await runtime.client.get(target, headers={"X-Fixture-Actor": "delegate2"}), 404)
        assert_private(await runtime.client.get(target.replace("org-a", "org-b"),
                                               headers={"X-Fixture-Actor": "delegate"}), 403)
    page: Final = await runtime.client.get(ROOT + "/reports", headers={"X-Fixture-Actor": "delegate2"})
    assert page.json()["items"] == [] and page.json()["total"] == 0
    assert_private(await runtime.client.get(GRANT, headers={"X-Fixture-Actor": "delegate"}), 403)
    assert_private(await grant(runtime, actor="delegate", version=1, key="self"), 403)


@pytest.mark.asyncio
async def test_same_creator_cannot_open_admin_audience_artifact_after_becoming_a_delegate(runtime: Runtime) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("INSERT INTO retail_organization_memberships "
                                 "(organization_id,user_id,role,approved_by) VALUES('org-a','other-admin','admin','admin')")
    assert_private(await grant(runtime, target="other-admin", key="grant-admin"), 200)
    created: Final = await runtime.client.post("/retail/admin/organizations/org-a/reports",
        json=runtime.window.model_dump(mode="json"), headers={"X-Fixture-Actor": "other-admin", "Idempotency-Key": "same"})
    assert_private(created, 202)
    job: Final = created.json()
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute("UPDATE \"LiteLLM_UserTable\" SET user_role='internal_user' WHERE user_id='other-admin'")
    path: Final = ROOT + "/reports/" + job["id"]
    assert_private(await runtime.client.get(path, headers={"X-Fixture-Actor": "other-admin"}), 404)
    assert_private(await runtime.client.get(path + "/download", headers={"X-Fixture-Actor": "other-admin"}), 404)
    own: Final = await runtime.client.get(ROOT + "/reports", headers={"X-Fixture-Actor": "other-admin"})
    assert own.json()["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ("viewer", "blocked", "password", "native-org-admin", "native-team"))
async def test_cached_actor_is_freshly_checked_even_for_existing_jobs_and_replays(runtime: Runtime, revocation: str) -> None:
    assert_private(await grant(runtime), 200)
    created: Final = await create(runtime)
    job: Final = created.json()
    queries: Final = {
        "viewer": "UPDATE \"LiteLLM_UserTable\" SET user_role='internal_user_viewer' WHERE user_id='delegate'",
        "blocked": "UPDATE \"LiteLLM_VerificationToken\" SET blocked=true WHERE user_id='delegate'",
        "password": "UPDATE \"LiteLLM_UserTable\" SET password_reset_required=true WHERE user_id='delegate'",
        "native-org-admin": "UPDATE \"LiteLLM_UserTable\" SET user_role='org_admin' WHERE user_id='delegate'",
        "native-team": "UPDATE \"LiteLLM_UserTable\" SET user_role='team' WHERE user_id='delegate'",
    }
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute(queries[revocation])
    for target in (SCOPES, ROOT + "/access", ROOT + "/consumption", ROOT + "/reports", ROOT + "/reports/" + job["id"]):
        assert_private(await runtime.client.get(target, headers={"X-Fixture-Actor": "delegate"}), 403)
    assert_private(await create(runtime), 403)
    assert_private(await runtime.client.get(ROOT + "/reports/" + job["id"] + "/download",
                                           headers={"X-Fixture-Actor": "delegate", "Range": "bytes=0-2"}), 403)


@pytest.mark.asyncio
async def test_admin_may_revoke_inactive_or_demoted_target_but_cannot_grant_native_viewer(runtime: Runtime) -> None:
    assert_private(await grant(runtime, target="readonly"), 404)
    assert_private(await grant(runtime), 200)
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_organization_memberships SET role='member',version=version+1 "
                                 "WHERE organization_id='org-a' AND user_id='delegate'")
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute("DELETE FROM \"LiteLLM_UserTable\" WHERE user_id='delegate'")
    revoked: Final = await grant(runtime, view=False, export=False, version=1, key="revoke-inactive")
    assert_private(revoked, 200)
    assert revoked.json()["state"] == "revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ("org_admin", "team"))
async def test_native_nonportal_target_is_rejected_before_a_grant_is_created(runtime: Runtime, role: str) -> None:
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute("UPDATE \"LiteLLM_UserTable\" SET user_role=%s WHERE user_id='delegate'", (role,))
    assert_private(await grant(runtime), 404)
    current: Final = await runtime.client.get(GRANT, headers={"X-Fixture-Actor": "admin"})
    assert_private(current, 200)
    assert current.json()["version"] == 0
    assert current.json()["can_view"] is False
    revoked: Final = await grant(runtime, view=False, export=False, key="revoke-unsupported")
    assert_private(revoked, 200)
    assert revoked.json()["state"] == "revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ("proxy_admin", "customer"))
async def test_supported_portal_roles_remain_eligible_for_explicit_finance_grants(runtime: Runtime, role: str) -> None:
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute("UPDATE \"LiteLLM_UserTable\" SET user_role=%s WHERE user_id='delegate'", (role,))
    selected: Final = await grant(runtime)
    assert_private(selected, 200)
    assert selected.json()["effective"] is True
    access: Final = await runtime.client.get(ROOT + "/consumption", headers={"X-Fixture-Actor": "delegate"})
    assert_private(access, 200)


@pytest.mark.asyncio
async def test_scope_id_filter_is_batched_exact_private_and_has_filtered_total(runtime: Runtime) -> None:
    assert_private(await grant(runtime), 200)
    page: Final = await runtime.client.get(SCOPES, params={"ids": "org-b,org-a,org-a,missing", "limit": 1},
                                          headers={"X-Fixture-Actor": "delegate"})
    assert_private(page, 200)
    assert page.json()["total"] == 1 and [row["id"] for row in page.json()["items"]] == ["org-a"]
    filtered: Final = await runtime.client.get(SCOPES, params={"ids": "org-b"}, headers={"X-Fixture-Actor": "delegate"})
    assert filtered.json()["total"] == 0 and filtered.json()["items"] == []
    for ids in ("", "org-a,", "*", "%", ",".join("org-"+str(index) for index in range(26))):
        assert_private(await runtime.client.get(SCOPES, params={"ids": ids}, headers={"X-Fixture-Actor": "delegate"}), 422)


@pytest.mark.asyncio
async def test_failed_report_never_projects_its_internal_error_code(runtime: Runtime) -> None:
    assert_private(await grant(runtime), 200)
    created: Final = await create(runtime)
    job: Final = created.json()
    async with runtime.reports.transaction() as connection:
        await connection.execute("UPDATE retail_delegated_report_jobs SET state='failed',error_code=%s WHERE id=%s",
                                 ("private-fixture-diagnostic", job["id"]))
    response: Final = await runtime.client.get(ROOT + "/reports/" + job["id"], headers={"X-Fixture-Actor": "delegate"})
    assert_private(response, 200)
    assert response.json()["state"] == "failed"
    assert "error_code" not in response.json()
    assert "private-fixture-diagnostic" not in response.text
    denied: Final = await runtime.client.get(ROOT + "/reports/" + job["id"] + "/download",
                                            headers={"X-Fixture-Actor": "delegate"})
    assert_private(denied, 409)
    assert "private-fixture-diagnostic" not in denied.text


@pytest.mark.asyncio
async def test_roster_existing_grant_marker_is_admin_only_and_survives_revocation(runtime: Runtime) -> None:
    assert_private(await grant(runtime), 200)
    assert_private(await grant(runtime, view=False, export=False, version=1, key="revoke"), 200)
    path: Final = "/retail/organizations/org-a/members"
    admin: Final = await runtime.client.get(path, headers={"X-Fixture-Actor": "admin"})
    assert_private(admin, 200)
    recipient: Final = next(item for item in admin.json()["items"] if item["user_id"] == "delegate")
    assert recipient["has_finance_grant"] is True
    delegate: Final = await runtime.client.get(path, headers={"X-Fixture-Actor": "delegate"})
    assert_private(delegate, 200)
    assert all("has_finance_grant" not in item for item in delegate.json()["items"])


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ("owner", "other", "org-admin", "viewer", "readonly"))
async def test_membership_or_native_viewer_does_not_implicitly_grant_finance(runtime: Runtime, actor: str) -> None:
    result: Final = await runtime.client.get(ROOT + "/consumption", headers={"X-Fixture-Actor": actor})
    assert_private(result, 403)
    assert_private(await create(runtime, actor=actor), 403)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ("RETAIL_ORG_FINANCE_DELEGATION_ENABLED", "RETAIL_ORG_FINANCE_ENABLED",
                                  "RETAIL_ORGANIZATION_DELEGATION_ENABLED", "RETAIL_ORGANIZATIONS_ENABLED",
                                  "RETAIL_OPERATION_AUDIT_ENABLED"))
async def test_feature_dependencies_close_delegated_and_admin_grant_endpoints(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, flag: str,
) -> None:
    monkeypatch.setenv(flag, "false")
    assert_private(await grant(runtime), 404)
    for path in (SCOPES, ROOT + "/access", ROOT + "/consumption", ROOT + "/reports"):
        assert_private(await runtime.client.get(path, headers={"X-Fixture-Actor": "delegate"}), 404)


@pytest.mark.asyncio
async def test_report_export_shutdown_keeps_view_available(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    assert_private(await grant(runtime), 200)
    monkeypatch.setenv("RETAIL_REPORT_EXPORT_ENABLED", "false")
    assert_private(await runtime.client.get(ROOT + "/consumption", headers={"X-Fixture-Actor": "delegate"}), 200)
    assert_private(await create(runtime), 404)


@pytest.mark.asyncio
async def test_delegated_live_cohort_overflow_rejects_a_partial_summary(runtime: Runtime) -> None:
    assert_private(await grant(runtime, export=False), 200)
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("""INSERT INTO retail_reservations(request_id,user_id,model,reserved_nanos)
            SELECT 'overflow-'||i,'owner','public-model',0 FROM generate_series(1,10001) i""")
        await connection.execute("""INSERT INTO retail_request_organization_snapshots
            SELECT request_id,'org-a',1,2,4,clock_timestamp() FROM retail_reservations""")
    response: Final = await runtime.client.get(ROOT + "/consumption", params={"limit": 1},
                                              headers={"X-Fixture-Actor": "delegate"})
    assert_private(response, 422)
    assert "summary" not in response.json()
    assert "smaller window" in response.text


@pytest.mark.asyncio
async def test_invalid_or_inconsistent_delegated_consumption_is_not_presented_as_zero(runtime: Runtime) -> None:
    assert_private(await grant(runtime, export=False), 200)
    invalid: Final = await runtime.client.get(ROOT + "/consumption", params={
        "start": runtime.window.start_at.isoformat(),
        "end": (runtime.window.start_at + timedelta(days=32)).isoformat(),
    }, headers={"X-Fixture-Actor": "delegate"})
    assert_private(invalid, 422)
    await seed(runtime)
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("DELETE FROM retail_ledger WHERE operation_key='charge:private-paid-id'")
    inconsistent: Final = await runtime.client.get(ROOT + "/consumption", headers={"X-Fixture-Actor": "delegate"})
    assert_private(inconsistent, 503)
    assert "summary" not in inconsistent.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ("false", 0, None))
async def test_grant_booleans_are_strict_and_errors_do_not_echo_values(runtime: Runtime, invalid: object) -> None:
    response: Final = await runtime.client.patch(GRANT,
        json={"can_view": invalid, "can_export": False, "expected_version": 0},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "invalid"})
    assert_private(response, 422)
    assert all("input" not in detail and "ctx" not in detail for detail in response.json()["detail"])


@pytest.mark.asyncio
async def test_unknown_query_scope_and_report_fields_are_rejected(runtime: Runtime) -> None:
    assert_private(await grant(runtime), 200)
    for path in (SCOPES, ROOT + "/access", ROOT + "/consumption", ROOT + "/reports"):
        response: Final = await runtime.client.get(path, params={"user_id": "private-fixture-user"},
                                                  headers={"X-Fixture-Actor": "delegate"})
        assert_private(response, 422)
        assert "private-fixture-user" not in response.text
    for field in ("account_id", "organization_id", "kind", "access_revision", "renderer"):
        response: Final = await runtime.client.post(ROOT + "/reports",
            json={**runtime.window.model_dump(mode="json"), field: "private-fixture-value"},
            headers={"X-Fixture-Actor": "delegate", "Idempotency-Key": "invalid"})
        assert_private(response, 422)
        assert "private-fixture-value" not in response.text


@pytest.mark.asyncio
async def test_grant_update_is_atomic_audited_and_cas_protected(runtime: Runtime) -> None:
    first, second = await asyncio.gather(grant(runtime, key="first"), grant(runtime, export=False, key="second"))
    assert sorted((first.status_code, second.status_code)) == [200, 409]
    winning: Final = first if first.status_code == 200 else second
    key: Final = "first" if first.status_code == 200 else "second"
    export: Final = key == "first"
    assert (await grant(runtime, key=key, export=export)).json() == winning.json()
    async with runtime.store.diagnostic_transaction() as connection:
        row: Final = await (await connection.execute("SELECT count(*) AS n FROM retail_operation_events "
            "WHERE action='finance_grant.updated'")).fetchone()
    assert row == {"n": 1}


@pytest.mark.asyncio
async def test_grant_audit_failure_rolls_back_permission(runtime: Runtime) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("""CREATE FUNCTION fixture_deny_grant_event() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'private-fixture-audit' USING ERRCODE='57014'; END $$""")
        await connection.execute("CREATE TRIGGER fixture_deny_grant_event BEFORE INSERT ON retail_operation_events "
                                 "FOR EACH ROW EXECUTE FUNCTION fixture_deny_grant_event()")
    failed: Final = await grant(runtime)
    assert_private(failed, 503)
    assert "private-fixture-audit" not in failed.text
    current: Final = await runtime.client.get(GRANT, headers={"X-Fixture-Actor": "admin"})
    assert current.json()["version"] == 0 and current.json()["effective"] is False


@pytest.mark.asyncio
async def test_finance_events_external_delivery_remains_seven_field_metadata(runtime: Runtime) -> None:
    delivery: Final = AuditDeliveryStore(runtime.store)
    try:
        await delivery.initialize()
        await delivery.configure(True, 1, "c" * 64, ControlPlaneActor("admin", "proxy_admin", True, True), "enable")
        assert_private(await grant(runtime), 200)
        created: Final = await create(runtime)
        assert_private(created, 202)
        await ready(runtime, created.json()["id"])
        async with runtime.store.diagnostic_transaction() as connection:
            rows: Final = await (await connection.execute("SELECT payload FROM retail_audit_deliveries")).fetchall()
        assert {row["payload"]["action"] for row in rows} == {
            "finance_grant.updated", "delegated_report.created", "delegated_report.completed",
        }
        for row in rows:
            assert set(row["payload"]) == {"event_id", "action", "resource_type", "result", "version_before",
                                           "version_after", "occurred_at"}
            assert "delegate" not in str(row["payload"]).replace("delegated_report", "report")
            assert "org-a" not in str(row["payload"])
    finally:
        await delivery.close()
