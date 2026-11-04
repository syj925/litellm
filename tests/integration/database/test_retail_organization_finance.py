import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from pydantic import JsonValue, TypeAdapter

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.retail import api, organization_finance
from litellm.proxy.retail.audit_delivery_store import AuditDeliveryStore
from litellm.proxy.retail.control_plane_access import ControlPlaneActor
from litellm.proxy.retail.organization_finance_store import BillingOrganizationUpdate, OrganizationFinanceStore
from litellm.proxy.retail.settlement_store import record_facts
from litellm.proxy.retail.store import WalletStore

ADMIN: Final = ControlPlaneActor("admin", "proxy_admin", True, True)
OWN: Final = ControlPlaneActor("owner", "internal_user", False, True)
OTHER: Final = ControlPlaneActor("other", "internal_user", False, True)
ROOT: Final = "/retail/admin/users/owner/billing-organization"
REPORT: Final = "/retail/admin/organizations/org-a/consumption"
_JSON: Final = TypeAdapter(Mapping[str, JsonValue])
_ITEMS: Final = TypeAdapter(tuple[Mapping[str, JsonValue], ...])


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
    peer: WalletStore
    client: httpx.AsyncClient
    native_url: str
    ends_at: datetime


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated organization-finance test PostgreSQL service is required")
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORGANIZATIONS_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORG_FINANCE_ENABLED", "true")
    base: Final = conninfo_to_dict(database_url)
    names: Final = ("retail_org_finance_" + uuid4().hex, "native_org_finance_" + uuid4().hex)
    with psycopg.connect(**base, autocommit=True) as connection:
        for name in names:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    address: Final = make_conninfo(**{**base, "dbname": names[0]})
    native_url: Final = make_conninfo(**{**base, "dbname": names[1]})
    store: Final = WalletStore(address, max_connections=4, diagnostic_connections=4, critical_connections=2)
    peer: Final = WalletStore(address, max_connections=4, diagnostic_connections=4, critical_connections=2)
    auths: Final = {
        user_id: UserAPIKeyAuth(
            user_id=user_id, api_key="fixture-" + user_id, user_role=role, team_id=UI_SESSION_TOKEN_TEAM_ID
        )
        for user_id, role in (
            ("owner", LitellmUserRoles.INTERNAL_USER),
            ("other", LitellmUserRoles.INTERNAL_USER),
            ("admin", LitellmUserRoles.PROXY_ADMIN),
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
    app.include_router(organization_finance.router)
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
        await peer.initialize()
        await store.initialize_control_plane()
        await peer.initialize_control_plane()
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
            yield Runtime(store, peer, client, native_url, clock["at"] + timedelta(days=1))
    finally:
        await asyncio.gather(store.aclose(), peer.aclose())
        with psycopg.connect(**base, autocommit=True) as connection:
            for name in names:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def assign(
    runtime: Runtime, organization_id: str | None = "org-a", *, version: int = 0, key: str = "assign"
) -> httpx.Response:
    return await runtime.client.patch(
        ROOT, json={"organization_id": organization_id, "expected_version": version},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": key},
    )


async def report(
    runtime: Runtime, *, start: str | None = None, end: str | None = None, limit: int = 25,
    offset: int = 0, extra: Mapping[str, str] | None = None,
) -> httpx.Response:
    params: Final = {
        **{key: value for key, value in (("start", start), ("end", end)) if value is not None},
        "limit": limit, "offset": offset, **(extra or {}),
    }
    return await runtime.client.get(REPORT, params=params, headers={"X-Fixture-Actor": "admin"})


async def reserve(runtime: Runtime, request_id: str) -> None:
    await runtime.store.reserve("owner", request_id, "public-model", "0.4", {}, max_parallel=100)


def assert_private(response: httpx.Response, status: int) -> None:
    assert response.status_code == status, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Vary"] == "Authorization, Cookie"


@pytest.mark.asyncio
async def test_assignment_is_explicit_versioned_audited_and_replays_original_receipt(runtime: Runtime) -> None:
    initial: Final = await runtime.client.get(ROOT, headers={"X-Fixture-Actor": "admin"})
    assert_private(initial, 200)
    assert initial.json()["version"] == 0
    assert initial.json()["state"] == "unassigned"
    eligible: Final = await runtime.client.get(ROOT + "/organizations", headers={"X-Fixture-Actor": "admin"})
    assert_private(eligible, 200)
    assert [item["id"] for item in eligible.json()["items"]] == ["org-a", "org-b"]
    selected: Final = await assign(runtime)
    assert_private(selected, 200)
    assert selected.json()["version"] == 1
    assert selected.json()["state"] == "active"
    assert selected.json()["organization_id"] == "org-a"
    cleared: Final = await assign(runtime, None, version=1, key="clear")
    assert_private(cleared, 200)
    assert cleared.json()["version"] == 2
    assert cleared.json()["state"] == "unassigned"
    assert (await assign(runtime)).json() == selected.json()
    assert_private(await assign(runtime, "org-b"), 409)
    async with runtime.store.diagnostic_transaction() as connection:
        events: Final = await (await connection.execute(
            "SELECT action,resource_type,resource_id,organization_id,version_before,version_after,changes "
            "FROM retail_operation_events ORDER BY created_at"
        )).fetchall()
    assert len(events) == 2
    assert tuple((event["version_before"], event["version_after"]) for event in events) == ((0, 1), (1, 2))
    assert events[0]["action"] == "billing_organization.updated"
    assert events[0]["resource_type"] == "billing_organization"
    assert events[0]["resource_id"] == "owner"
    assert events[0]["organization_id"] is None
    assert events[0]["changes"] == {"user_id": "owner", "organization_id": "org-a"}


@pytest.mark.asyncio
@pytest.mark.parametrize("organization_id", ("org-disabled", "org-ineligible", "does-not-exist"))
async def test_assignment_rejects_ineligible_organizations_without_creating_a_config(
    runtime: Runtime, organization_id: str
) -> None:
    assert_private(await assign(runtime, organization_id), 422)
    current: Final = await runtime.client.get(ROOT, headers={"X-Fixture-Actor": "admin"})
    assert current.json()["version"] == 0
    assert current.json()["organization_id"] is None


@pytest.mark.asyncio
async def test_concurrent_updates_use_wallet_lock_and_compare_version(runtime: Runtime) -> None:
    first, second = await asyncio.gather(
        OrganizationFinanceStore(runtime.store).update(
            "owner", BillingOrganizationUpdate(organization_id="org-a", expected_version=0), ADMIN, "first"
        ),
        OrganizationFinanceStore(runtime.peer).update(
            "owner", BillingOrganizationUpdate(organization_id="org-b", expected_version=0), ADMIN, "second"
        ),
        return_exceptions=True,
    )
    successes: Final = tuple(value for value in (first, second) if isinstance(value, Mapping))
    failures: Final = tuple(value for value in (first, second) if isinstance(value, HTTPException))
    assert len(successes) == 1
    assert successes[0]["version"] == 1
    assert len(failures) == 1
    assert failures[0].status_code == 409


@pytest.mark.asyncio
async def test_concurrent_same_key_replays_one_mutation_and_one_audit(runtime: Runtime) -> None:
    data: Final = BillingOrganizationUpdate(organization_id="org-a", expected_version=0)
    first, second = await asyncio.gather(
        OrganizationFinanceStore(runtime.store).update("owner", data, ADMIN, "same"),
        OrganizationFinanceStore(runtime.peer).update("owner", data, ADMIN, "same"),
    )
    assert first == second
    assert first["version"] == 1
    async with runtime.store.diagnostic_transaction() as connection:
        event: Final = await (await connection.execute("SELECT count(*) AS n FROM retail_operation_events")).fetchone()
    assert event is not None
    assert event["n"] == 1


@pytest.mark.asyncio
async def test_eligible_picker_search_is_literal_and_excludes_disabled_memberships(runtime: Runtime) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_organizations SET name='Literal_%' WHERE id='org-a'")
        await connection.execute("UPDATE retail_organization_memberships SET enabled=false "
                                 "WHERE organization_id='org-b' AND user_id='owner'")
    found: Final = await runtime.client.get(ROOT + "/organizations", params={"q": "_%", "limit": 1},
                                           headers={"X-Fixture-Actor": "admin"})
    assert_private(found, 200)
    assert found.json()["total"] == 1
    assert found.json()["items"] == [{"id": "org-a", "name": "Literal_%", "organization_version": 2,
                                       "membership_version": 4}]
    empty_page: Final = await runtime.client.get(ROOT + "/organizations", params={"q": "_%", "offset": 1},
                                                headers={"X-Fixture-Actor": "admin"})
    assert empty_page.json()["total"] == 1
    assert empty_page.json()["items"] == []


@pytest.mark.asyncio
async def test_audit_storage_failure_rolls_back_assignment(runtime: Runtime) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("""CREATE FUNCTION fixture_reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'fixture-storage-failure' USING ERRCODE='57014'; END $$""")
        await connection.execute("CREATE TRIGGER fixture_reject_audit BEFORE INSERT ON retail_operation_events "
                                 "FOR EACH ROW EXECUTE FUNCTION fixture_reject_audit()")
    failed: Final = await assign(runtime)
    assert_private(failed, 503)
    assert "fixture-storage-failure" not in failed.text
    current: Final = await runtime.client.get(ROOT, headers={"X-Fixture-Actor": "admin"})
    assert current.json()["version"] == 0
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("DROP TRIGGER fixture_reject_audit ON retail_operation_events")
    assert_private(await assign(runtime), 200)


@pytest.mark.asyncio
async def test_assignment_delivery_contains_only_audit_metadata(runtime: Runtime) -> None:
    delivery: Final = AuditDeliveryStore(runtime.store)
    fingerprint: Final = "a" * 64
    try:
        await delivery.initialize()
        await delivery.configure(True, 1, fingerprint, ADMIN, "enable-delivery")
        assert_private(await assign(runtime), 200)
        claim: Final = await delivery.claim("fixture-worker", fingerprint)
        assert claim is not None
        envelope: Final = claim.payload.model_dump(mode="json")
        assert set(envelope) == {"event_id", "action", "resource_type", "result", "version_before",
                                 "version_after", "occurred_at"}
        assert envelope["action"] == "billing_organization.updated"
        assert envelope["resource_type"] == "billing_organization"
        assert envelope["version_before"] == 0
        assert envelope["version_after"] == 1
        assert "owner" not in str(envelope)
        assert "org-a" not in str(envelope)
    finally:
        await delivery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ("owner", "other", "viewer", "org-admin"))
async def test_only_writable_proxy_admin_can_read_assign_or_report(runtime: Runtime, actor: str) -> None:
    headers: Final = {"X-Fixture-Actor": actor, "Idempotency-Key": "denied"}
    assert_private(await runtime.client.get(ROOT, headers=headers), 403)
    assert_private(await runtime.client.get(ROOT + "/organizations", headers=headers), 403)
    assert_private(await runtime.client.get(REPORT, headers=headers), 403)
    assert_private(await runtime.client.patch(
        ROOT, json={"organization_id": "org-a", "expected_version": 0}, headers=headers,
    ), 403)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ("role", "credential", "password"))
async def test_fresh_authority_revocation_blocks_reads_and_idempotent_replay(
    runtime: Runtime, revocation: str
) -> None:
    assert_private(await assign(runtime), 200)
    queries: Final = {
        "role": "UPDATE \"LiteLLM_UserTable\" SET user_role='internal_user' WHERE user_id='admin'",
        "credential": "UPDATE \"LiteLLM_VerificationToken\" SET blocked=true WHERE user_id='admin'",
        "password": "UPDATE \"LiteLLM_UserTable\" SET password_reset_required=true WHERE user_id='admin'",
    }
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute(queries[revocation])
    assert_private(await runtime.client.get(ROOT, headers={"X-Fixture-Actor": "admin"}), 403)
    assert_private(await assign(runtime), 403)
    assert_private(await report(runtime), 403)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ("RETAIL_OPERATION_AUDIT_ENABLED", "RETAIL_ORGANIZATIONS_ENABLED",
                                  "RETAIL_ORG_FINANCE_ENABLED"))
async def test_feature_dependency_off_hides_all_routes(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    monkeypatch.setenv(flag, "false")
    assert_private(await runtime.client.get(ROOT, headers={"X-Fixture-Actor": "admin"}), 404)
    assert_private(await assign(runtime), 404)
    assert_private(await report(runtime), 404)


@pytest.mark.asyncio
async def test_report_freezes_admission_organization_and_sums_whole_cohort(runtime: Runtime) -> None:
    await reserve(runtime, "before-assignment")
    assert_private(await assign(runtime), 200)
    for request_id in ("paid", "zero", "released", "pending", "unknown", "conflict"):
        await reserve(runtime, request_id)
    assert_private(await assign(runtime, "org-b", version=1, key="move"), 200)
    await runtime.store.settle("paid", "0.123456789", {"api_key": "must-not-appear", "provider_cost": "9"})
    await runtime.store.settle("zero", "0", {})
    await runtime.store.release("released", "fixture")
    await record_facts(runtime.store, "unknown", None, {"billing_error": "private-upstream-error"})
    await record_facts(runtime.store, "conflict", "0.2", {})
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_settlement_facts SET state='conflict' WHERE request_id='conflict'")
    await reserve(runtime, "after-move")
    await runtime.store.settle("after-move", "0.9", {})
    first: Final = await report(runtime, limit=1)
    assert_private(first, 200)
    payload: Final = first.json()
    assert payload["axis"] == "admission"
    assert payload["live"] is True
    assert payload["total"] == 6
    assert payload["summary"] == {
        "accepted_requests": 6, "settled_requests": 2, "released_requests": 1, "pending_requests": 1,
        "unknown_requests": 1, "conflict_requests": 1, "settled_usd": "0.123456789", "settled_nanos": "123456789",
    }
    assert len(payload["items"]) == 1
    later: Final = await report(runtime, limit=1, offset=5)
    assert later.json()["summary"] == payload["summary"]
    assert later.json()["items"][0]["request_id"] != payload["items"][0]["request_id"]
    complete: Final = (await report(runtime)).json()["items"]
    assert {item["request_id"] for item in complete} == {"paid", "zero", "released", "pending", "unknown", "conflict"}
    assert all(item["assignment_version"] == 1 for item in complete)
    assert all(item["organization_version"] == 2 and item["membership_version"] == 4 for item in complete)
    assert {item["request_id"]: item["settled_usd"] for item in complete} == {
        "paid": "0.123456789", "zero": "0.000000000", "released": None, "pending": None,
        "unknown": None, "conflict": None,
    }
    assert all(set(item) == {"request_id", "user_id", "model", "captured_at", "state", "settled_usd",
                             "assignment_version", "organization_version", "membership_version"} for item in complete)
    assert "must-not-appear" not in first.text
    assert "private-upstream-error" not in first.text


@pytest.mark.asyncio
async def test_disabled_membership_and_organization_keep_choice_but_stop_future_capture(runtime: Runtime) -> None:
    assert_private(await assign(runtime), 200)
    await reserve(runtime, "captured")
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_organization_memberships SET enabled=false,version=version+1 "
                                 "WHERE organization_id='org-a' AND user_id='owner'")
    current: Final = await runtime.client.get(ROOT, headers={"X-Fixture-Actor": "admin"})
    assert current.json()["state"] == "membership_inactive"
    assert current.json()["organization_id"] == "org-a"
    await reserve(runtime, "member-disabled")
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_organizations SET enabled=false,version=version+1 WHERE id='org-a'")
    disabled: Final = await runtime.client.get(ROOT, headers={"X-Fixture-Actor": "admin"})
    assert disabled.json()["state"] == "organization_disabled"
    await reserve(runtime, "org-disabled")
    historical: Final = await report(runtime)
    assert_private(historical, 200)
    assert historical.json()["organization_enabled"] is False
    assert historical.json()["total"] == 1
    assert historical.json()["items"][0]["request_id"] == "captured"


@pytest.mark.asyncio
async def test_report_uses_admission_time_and_exclusive_end_instead_of_ledger_time(runtime: Runtime) -> None:
    captured: Final = datetime(2025, 1, 2, 0, tzinfo=timezone.utc)
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("INSERT INTO retail_reservations(request_id,user_id,model,reserved_nanos) "
                                 "VALUES('historical','owner','public-model',0)")
        await connection.execute("INSERT INTO retail_request_organization_snapshots "
                                 "VALUES('historical','org-a',1,2,4,%s)", (captured,))
    await runtime.store.settle("historical", "0.2", {})
    in_window: Final = await report(runtime, start=captured.isoformat(), end=(captured + timedelta(seconds=1)).isoformat())
    assert_private(in_window, 200)
    assert in_window.json()["summary"]["settled_usd"] == "0.200000000"
    assert in_window.json()["partial"] is False
    excluded: Final = await report(runtime, start=(captured - timedelta(days=1)).isoformat(), end=captured.isoformat())
    assert excluded.json()["total"] == 0
    assert excluded.json()["summary"]["settled_nanos"] == "0"


@pytest.mark.asyncio
async def test_cohort_over_limit_rejects_instead_of_returning_partial_or_zero_summary(runtime: Runtime) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("""INSERT INTO retail_reservations(request_id,user_id,model,reserved_nanos)
            SELECT 'bulk-'||i,'owner','public-model',0 FROM generate_series(1,10001) i""")
        await connection.execute("""INSERT INTO retail_request_organization_snapshots
            SELECT request_id,'org-a',1,2,4,clock_timestamp() FROM retail_reservations""")
    response: Final = await report(runtime, limit=1)
    assert_private(response, 422)
    assert "smaller window" in response.text
    assert "summary" not in response.json()


@pytest.mark.asyncio
async def test_exact_cohort_limit_returns_whole_total_even_when_page_is_empty(runtime: Runtime) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("""INSERT INTO retail_reservations(request_id,user_id,model,reserved_nanos)
            SELECT 'bulk-'||i,'owner','public-model',0 FROM generate_series(1,10000) i""")
        await connection.execute("""INSERT INTO retail_request_organization_snapshots
            SELECT request_id,'org-a',1,2,4,clock_timestamp() FROM retail_reservations""")
    response: Final = await report(runtime, limit=100, offset=10000)
    assert_private(response, 200)
    assert response.json()["total"] == 10000
    assert response.json()["summary"]["pending_requests"] == 10000
    assert response.json()["summary"]["settled_nanos"] == "0"
    assert response.json()["items"] == []


@pytest.mark.asyncio
async def test_inconsistent_settlement_cannot_be_reported_as_zero_charge(runtime: Runtime) -> None:
    assert_private(await assign(runtime), 200)
    await reserve(runtime, "corrupt")
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_reservations SET status='settled',settled_nanos=1 WHERE request_id='corrupt'")
    response: Final = await report(runtime)
    assert_private(response, 503)
    assert "summary" not in response.json()


@pytest.mark.asyncio
async def test_private_validation_does_not_echo_extra_or_malformed_inputs(runtime: Runtime) -> None:
    private: Final = "private-fixture-value"
    malformed: Final = await runtime.client.patch(
        ROOT, json={"organization_id": "org-a", "expected_version": private, "upstream_secret": private},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "invalid"},
    )
    assert_private(malformed, 422)
    assert private not in malformed.text
    assert_private(await report(runtime, extra={"secret": private}), 422)
    assert_private(await report(runtime, start="2025-01-01T00:00:00", end="2025-01-02T00:00:00"), 422)
    assert_private(await runtime.client.get(ROOT + "/organizations", params={"limit": 101},
                                            headers={"X-Fixture-Actor": "admin"}), 422)
    assert_private(await runtime.client.get(ROOT.replace("owner", "no-wallet"),
                                            headers={"X-Fixture-Actor": "admin"}), 404)
    assert_private(await runtime.client.get(REPORT.replace("org-a", "missing"),
                                            headers={"X-Fixture-Actor": "admin"}), 404)
