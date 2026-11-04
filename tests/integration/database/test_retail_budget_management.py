import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

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
from litellm.proxy.retail import api, model_budgets
from litellm.proxy.retail.budget_management import (
    BudgetManagementStore,
    CreateModelBudget,
    ScheduleModelBudget,
    UpdateModelBudget,
)
from litellm.proxy.retail.control_plane_access import ControlPlaneActor
from litellm.proxy.retail.models import BudgetExceeded
from litellm.proxy.retail.store import WalletStore

ADMIN: Final = ControlPlaneActor("admin", "proxy_admin", True, True)
OWN: Final = ControlPlaneActor("owner", "internal_user", False, True)
OTHER: Final = ControlPlaneActor("other", "internal_user", False, True)
ROOT: Final = "/retail/admin/users/owner/model-budgets"
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
class ModelRouter:
    model_names: frozenset[str] = frozenset(("public-model", "vendor/public-alias", "literal_%", "wildcard-*"))


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
        pytest.skip("A dedicated budget-management test PostgreSQL service is required")
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_MODEL_BUDGETS_ENABLED", "true")
    base: Final = conninfo_to_dict(database_url)
    names: Final = ("retail_budget_mgmt_" + uuid4().hex, "native_budget_mgmt_" + uuid4().hex)
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
            ("viewer", LitellmUserRoles.INTERNAL_USER_VIEW_ONLY),
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
    app.include_router(model_budgets.router)
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
        monkeypatch.setattr(proxy_server, "llm_router", ModelRouter())
        await store.initialize()
        await peer.initialize()
        await store.initialize_control_plane()
        await peer.initialize_control_plane()
        for user_id in auths:
            await store.get_wallet(user_id, create=True)
            await store.credit(user_id, "100", "seed-" + user_id, "fixture", "fixture", "seed-" + user_id)
        async with store.diagnostic_transaction() as connection:
            clock: Final = await (await connection.execute("SELECT clock_timestamp() AS at")).fetchone()
        assert clock is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            yield Runtime(store, peer, client, native_url, clock["at"] + timedelta(days=1))
    finally:
        await asyncio.gather(store.aclose(), peer.aclose())
        with psycopg.connect(**base, autocommit=True) as connection:
            for name in names:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _body(
    runtime: Runtime, *, model: str = "public-model", limit: str = "1", enabled: bool = True
) -> Mapping[str, JsonValue]:
    return {"model": model, "limit_usd": limit, "enabled": enabled, "ends_at": runtime.ends_at.isoformat()}


async def _create(
    runtime: Runtime, *, key: str = "create", model: str = "public-model", enabled: bool = True
) -> Mapping[str, JsonValue]:
    response: Final = await runtime.client.post(
        ROOT,
        json=_body(runtime, model=model, enabled=enabled),
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return _JSON.validate_python(response.json())


def _identifier(result: Mapping[str, JsonValue]) -> UUID:
    value: Final = result["id"]
    assert isinstance(value, str)
    return UUID(value)


def _successor_body(runtime: Runtime, *, enabled: bool = True) -> Mapping[str, JsonValue]:
    return {
        "expected_version": 1,
        "ends_at": (runtime.ends_at + timedelta(days=1)).isoformat(),
        "limit_usd": "2",
        "enabled": enabled,
    }


async def _schedule(
    runtime: Runtime, parent: Mapping[str, JsonValue], *, enabled: bool = True, key: str = "schedule"
) -> Mapping[str, JsonValue]:
    response: Final = await runtime.client.post(
        ROOT + "/" + str(parent["id"]) + "/successor",
        json=_successor_body(runtime, enabled=enabled),
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return _JSON.validate_python(response.json())


@pytest.mark.asyncio
async def test_admin_creation_is_server_clock_stamped_audited_and_personal_projection_is_private(
    runtime: Runtime,
) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        before: Final = await (await connection.execute("SELECT clock_timestamp() AS at")).fetchone()
    result: Final = await _create(runtime)
    assert before is not None
    start: Final = result["effective_from"]
    assert isinstance(start, str) and datetime.fromisoformat(start.replace("Z", "+00:00")) >= before["at"]
    assert result["created_at"] == start and result["updated_at"] == start
    assert (result["config_version"], result["accounting_version"], result["accounted_at"]) == (1, 0, None)
    own: Final = await runtime.client.get("/retail/model-budgets", headers={"X-Fixture-Actor": "owner"})
    other: Final = await runtime.client.get("/retail/model-budgets", headers={"X-Fixture-Actor": "other"})
    own_rows: Final = _ITEMS.validate_python(own.json()["items"])
    assert own.status_code == 200 and len(own_rows) == 1 and other.json()["total"] == 0
    assert set(result) - set(own_rows[0]) == {
        "user_id",
        "source",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
    }
    assert "no-store" in own.headers["Cache-Control"] and "Authorization" in own.headers["Vary"]
    async with runtime.store.diagnostic_transaction() as connection:
        event: Final = await (await connection.execute("SELECT * FROM retail_operation_events")).fetchone()
    assert event is not None
    assert event["action"] == "model_budget.created" and event["organization_id"] is None
    assert event["response"] == {"id": result["id"], "config_version": 1}


@pytest.mark.asyncio
async def test_two_store_retries_have_one_policy_one_receipt_and_one_audit_event(runtime: Runtime) -> None:
    data: Final = CreateModelBudget.model_validate(_body(runtime))
    responses: Final = await asyncio.gather(
        *(
            BudgetManagementStore(runtime.store if index % 2 else runtime.peer).create("owner", data, ADMIN, "retry")
            for index in range(6)
        )
    )
    assert all(row == responses[0] for row in responses)
    async with runtime.store.diagnostic_transaction() as connection:
        counts: Final = await (
            await connection.execute("""SELECT (SELECT count(*) FROM retail_model_budget_policies) AS policies,
            (SELECT count(*) FROM retail_operation_events) AS events,
            (SELECT count(*) FROM retail_model_budget_management_receipts) AS receipts""")
        ).fetchone()
    assert counts == {"policies": 1, "events": 1, "receipts": 1}
    conflict: Final = await runtime.client.post(
        ROOT, json=_body(runtime, limit="2"), headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "retry"}
    )
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_disabled_windows_still_prevent_overlap_while_other_accounts_are_independent(runtime: Runtime) -> None:
    await _create(runtime, enabled=False)
    overlap: Final = await runtime.client.post(
        ROOT, json=_body(runtime), headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "overlap"}
    )
    assert overlap.status_code == 409 and "overlap" in overlap.text
    other: Final = await runtime.client.post(
        ROOT.replace("owner", "other"),
        json=_body(runtime),
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "other-account"},
    )
    assert other.status_code == 201
    async with runtime.store.diagnostic_transaction() as connection:
        count: Final = await (
            await connection.execute("SELECT count(*) AS total FROM retail_operation_events")
        ).fetchone()
    assert count == {"total": 2}


@pytest.mark.asyncio
async def test_financial_updates_do_not_conflict_with_config_cas_or_reset_usage_when_limit_is_lowered(
    runtime: Runtime,
) -> None:
    created: Final = await _create(runtime)
    await runtime.store.reserve("owner", "request", "public-model", "0.5", {})
    await runtime.store.settle("request", "0.3", {})
    changed: Final = await runtime.client.patch(
        ROOT + "/" + str(created["id"]),
        json={"limit_usd": "0.1", "expected_version": 1},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "reduce"},
    )
    assert changed.status_code == 200, changed.text
    result: Final = changed.json()
    assert (result["config_version"], result["accounting_version"], result["spent_usd"], result["held_usd"]) == (
        2,
        2,
        "0.300000000",
        "0.000000000",
    )
    assert result["remaining_usd"] == "0.000000000" and result["overrun_usd"] == "0.200000000"
    with pytest.raises(BudgetExceeded):
        await runtime.peer.reserve("owner", "later", "public-model", "0.01", {})
    replay: Final = await runtime.client.patch(
        ROOT + "/" + str(created["id"]),
        json={"limit_usd": "0.1", "expected_version": 1},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "reduce"},
    )
    assert replay.json() == result


@pytest.mark.asyncio
async def test_concurrent_configuration_cas_has_one_winner_and_wrong_scope_is_404(runtime: Runtime) -> None:
    created: Final = await _create(runtime)
    data: Final = UpdateModelBudget(enabled=False, expected_version=1)
    results: Final = await asyncio.gather(
        *(
            BudgetManagementStore(store).update("owner", _identifier(created), data, ADMIN, "change-" + str(index))
            for index, store in enumerate((runtime.store, runtime.peer))
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(row, Mapping) for row in results) == 1
    assert sum(isinstance(row, HTTPException) and row.status_code == 409 for row in results) == 1
    wrong: Final = await runtime.client.patch(
        ROOT.replace("owner", "other") + "/" + str(created["id"]),
        json={"enabled": False, "expected_version": 1},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "wrong-scope"},
    )
    assert wrong.status_code == 404
    with pytest.raises(HTTPException) as foreign:
        await BudgetManagementStore(runtime.store).list("owner", OTHER, admin=False)
    assert foreign.value.status_code == 404


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_budget_creation_update_and_private_receipt(runtime: Runtime) -> None:
    created: Final = await _create(runtime)
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("""CREATE FUNCTION fixture_fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'fixture audit unavailable'; END; $$;
            CREATE TRIGGER fixture_fail_audit BEFORE INSERT ON retail_operation_events
            FOR EACH ROW EXECUTE FUNCTION fixture_fail_audit();""")
    failed_create: Final = await runtime.client.post(
        ROOT,
        json=_body(runtime, model="vendor/public-alias"),
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "fail-create"},
    )
    failed_update: Final = await runtime.client.patch(
        ROOT + "/" + str(created["id"]),
        json={"enabled": False, "expected_version": 1},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "fail-update"},
    )
    failed_schedule: Final = await runtime.client.post(
        ROOT + "/" + str(created["id"]) + "/successor",
        json=_successor_body(runtime),
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "fail-schedule"},
    )
    assert failed_create.status_code == failed_update.status_code == failed_schedule.status_code == 503
    assert "fixture" not in failed_create.text and "fixture" not in failed_update.text
    assert "fixture" not in failed_schedule.text
    async with runtime.store.diagnostic_transaction() as connection:
        current: Final = await (
            await connection.execute("SELECT config_version,enabled FROM retail_model_budget_policies")
        ).fetchall()
        count: Final = await (
            await connection.execute("SELECT count(*) AS total FROM retail_model_budget_management_receipts")
        ).fetchone()
    assert current == [{"config_version": 1, "enabled": True}] and count == {"total": 1}


@pytest.mark.asyncio
async def test_only_configured_exact_public_names_are_selectable_with_bounded_literal_search(runtime: Runtime) -> None:
    picker: Final = await runtime.client.get(
        ROOT + "/models?q=PUBLIC&limit=1&offset=1", headers={"X-Fixture-Actor": "admin"}
    )
    assert picker.status_code == 200 and picker.json() == {
        "items": [{"model": "vendor/public-alias"}],
        "total": 2,
        "limit": 1,
        "offset": 1,
    }
    denied: Final = await runtime.client.post(
        ROOT,
        json=_body(runtime, model="upstream-deployment-id"),
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "private-model"},
    )
    assert denied.status_code == 422
    await _create(runtime, model="literal_%")
    await _create(runtime, key="alias", model="vendor/public-alias")
    matching: Final = await runtime.client.get(ROOT + "?q=_%&limit=1", headers={"X-Fixture-Actor": "admin"})
    assert matching.json()["total"] == 1 and matching.json()["items"][0]["model"] == "literal_%"
    page: Final = await runtime.client.get(ROOT + "?limit=1&offset=1", headers={"X-Fixture-Actor": "admin"})
    assert page.json()["total"] == 2 and len(page.json()["items"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ("no-wallet", "no-native-account"))
async def test_missing_native_account_or_wallet_is_not_created_by_management(runtime: Runtime, account: str) -> None:
    response: Final = await runtime.client.post(
        ROOT.replace("owner", account),
        json=_body(runtime),
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "unknown"},
    )
    assert response.status_code == 404
    async with runtime.store.diagnostic_transaction() as connection:
        count: Final = await (
            await connection.execute("SELECT count(*) AS total FROM retail_wallets WHERE user_id=%s", (account,))
        ).fetchone()
    assert count == {"total": 0}


@pytest.mark.asyncio
async def test_expired_windows_reject_new_edits_but_keep_original_success_receipts(runtime: Runtime) -> None:
    data: Final = CreateModelBudget(
        model="public-model", ends_at=datetime.now().astimezone() + timedelta(seconds=1), limit_usd="1"
    )
    created: Final = await BudgetManagementStore(runtime.store).create("owner", data, ADMIN, "short")
    await asyncio.sleep(1.1)
    replay: Final = await BudgetManagementStore(runtime.peer).create("owner", data, ADMIN, "short")
    assert replay == created
    page: Final = await BudgetManagementStore(runtime.store).list("owner", OWN, admin=False)
    assert _ITEMS.validate_python(page["items"])[0]["state"] == "expired"
    with pytest.raises(HTTPException) as expired:
        await BudgetManagementStore(runtime.store).update(
            "owner", _identifier(created), UpdateModelBudget(enabled=False, expected_version=1), ADMIN, "after-end"
        )
    assert expired.value.status_code == 409


@pytest.mark.asyncio
async def test_patch_waiting_on_idempotency_lock_cannot_edit_a_window_after_it_expires(runtime: Runtime) -> None:
    async with runtime.store.diagnostic_transaction() as clock_connection:
        clock: Final = await (await clock_connection.execute("SELECT clock_timestamp() AS at")).fetchone()
    assert clock is not None
    end: Final = clock["at"] + timedelta(seconds=0.6)
    created: Final = await BudgetManagementStore(runtime.store).create(
        "owner",
        CreateModelBudget(model="public-model", ends_at=end, limit_usd="1"),
        ADMIN,
        "short-window",
    )
    async with runtime.store.diagnostic_transaction() as blocker:
        await blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("retail:operation:admin:late-edit",)
        )
        pending: Final = asyncio.create_task(
            BudgetManagementStore(runtime.peer).update(
                "owner",
                _identifier(created),
                UpdateModelBudget(enabled=False, expected_version=1),
                ADMIN,
                "late-edit",
            )
        )
        try:
            for _ in range(100):
                waiting: Final = await (
                    await blocker.execute("""SELECT count(*) AS total FROM pg_locks
                    WHERE locktype='advisory' AND NOT granted
                    AND database=(SELECT oid FROM pg_database WHERE datname=current_database())""")
                ).fetchone()
                if waiting is not None and waiting["total"] > 0:
                    break
                await asyncio.sleep(0.002)
            else:
                pytest.fail("The update never reached the held idempotency lock")
            await blocker.execute(
                "SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM (%s::timestamptz-clock_timestamp())))+0.03)", (end,)
            )
        except BaseException:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            raise
    with pytest.raises(HTTPException) as expired:
        await pending
    assert expired.value.status_code == 409
    async with runtime.store.diagnostic_transaction() as connection:
        row: Final = await (
            await connection.execute("SELECT enabled,config_version FROM retail_model_budget_policies")
        ).fetchone()
        events: Final = await (
            await connection.execute("SELECT count(*) AS total FROM retail_operation_events")
        ).fetchone()
    assert row == {"enabled": True, "config_version": 1} and events == {"total": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("role", "credential", "account"))
async def test_live_native_revocation_blocks_cached_admin_grant_including_idempotent_replay(
    runtime: Runtime, change: str
) -> None:
    parent: Final = await _create(runtime)
    await _schedule(runtime, parent)
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        if change == "role":
            await connection.execute("UPDATE \"LiteLLM_UserTable\" SET user_role='internal_user' WHERE user_id='admin'")
        elif change == "credential":
            await connection.execute("UPDATE \"LiteLLM_VerificationToken\" SET blocked=true WHERE user_id='admin'")
        else:
            await connection.execute(
                "UPDATE \"LiteLLM_UserTable\" SET password_reset_required=true WHERE user_id='admin'"
            )
    denied: Final = await runtime.client.post(
        ROOT, json=_body(runtime), headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "create"}
    )
    assert denied.status_code == 403 and "no-store" in denied.headers["Cache-Control"]
    successor_replay: Final = await runtime.client.post(
        ROOT + "/" + str(parent["id"]) + "/successor",
        json=_successor_body(runtime),
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "schedule"},
    )
    assert successor_replay.status_code == 403 and "no-store" in successor_replay.headers["Cache-Control"]
    page: Final = await runtime.client.get(ROOT, headers={"X-Fixture-Actor": "admin"})
    assert page.status_code == 403


@pytest.mark.asyncio
async def test_ordinary_and_viewer_only_read_their_own_budget_and_reject_scope_injection(runtime: Runtime) -> None:
    await _create(runtime)
    viewer: Final = await runtime.client.get("/retail/model-budgets", headers={"X-Fixture-Actor": "viewer"})
    assert viewer.status_code == 200 and viewer.json()["total"] == 0
    for role in ("owner", "viewer"):
        denied: Final = await runtime.client.post(
            ROOT, json=_body(runtime), headers={"X-Fixture-Actor": role, "Idempotency-Key": "not-admin"}
        )
        assert denied.status_code == 403
    injected: Final = await runtime.client.get(
        "/retail/model-budgets?user_id=other", headers={"X-Fixture-Actor": "owner"}
    )
    assert injected.status_code == 422 and "no-store" in injected.headers["Cache-Control"]


@pytest.mark.asyncio
async def test_feature_rollback_closes_management_but_cannot_disable_existing_budget_enforcement(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent: Final = await _create(runtime)
    monkeypatch.setenv("RETAIL_MODEL_BUDGETS_ENABLED", "false")
    config: Final = await runtime.client.get("/retail/config")
    assert config.json()["model_budgets_enabled"] is False
    page: Final = await runtime.client.get("/retail/model-budgets", headers={"X-Fixture-Actor": "owner"})
    create: Final = await runtime.client.post(
        ROOT, json=_body(runtime), headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "off"}
    )
    schedule: Final = await runtime.client.post(
        ROOT + "/" + str(parent["id"]) + "/successor",
        json=_successor_body(runtime),
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "schedule-off"},
    )
    assert page.status_code == create.status_code == schedule.status_code == 404
    with pytest.raises(BudgetExceeded):
        await runtime.peer.reserve("owner", "too-large", "public-model", "2", {})


@pytest.mark.asyncio
async def test_native_authority_failure_is_safe_503_even_for_success_receipt_replay(runtime: Runtime) -> None:
    await _create(runtime)
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute('DROP TABLE "LiteLLM_VerificationToken"')
    replay: Final = await runtime.client.post(
        ROOT, json=_body(runtime), headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "create"}
    )
    own: Final = await runtime.client.get("/retail/model-budgets", headers={"X-Fixture-Actor": "owner"})
    assert own.status_code == replay.status_code == 503 and "LiteLLM" not in replay.text


@pytest.mark.asyncio
async def test_private_validation_errors_omit_submitted_payload_and_reject_unbounded_queries(runtime: Runtime) -> None:
    invalid: Final = await runtime.client.post(
        ROOT,
        json={**_body(runtime), "limit_usd": "private-invalid-input"},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "invalid"},
    )
    assert invalid.status_code == 422 and "private-invalid-input" not in invalid.text
    assert "no-store" in invalid.headers["Cache-Control"]
    for query in ("limit=101", "offset=10001", "q=" + "x" * 101):
        page: Final = await runtime.client.get(ROOT + "?" + query, headers={"X-Fixture-Actor": "admin"})
        assert page.status_code == 422 and "no-store" in page.headers["Cache-Control"]


@pytest.mark.asyncio
@pytest.mark.parametrize("days", (-1, 367))
async def test_creation_rejects_past_or_excessive_windows_from_server_clock(runtime: Runtime, days: int) -> None:
    response: Final = await runtime.client.post(
        ROOT,
        json={**_body(runtime), "ends_at": (runtime.ends_at + timedelta(days=days - 1)).isoformat()},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "bad-window"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", (True, False))
async def test_successor_starts_at_parent_end_with_zero_usage_and_private_linked_projection(
    runtime: Runtime, enabled: bool
) -> None:
    parent: Final = await _create(runtime, enabled=enabled)
    await runtime.store.reserve("owner", "held-old", "public-model", "0.25", {})
    child: Final = await _schedule(runtime, parent, enabled=enabled)
    assert (child["predecessor_id"], child["successor_id"], child["effective_from"], child["state"]) == (
        parent["id"], None, parent["ends_at"], "scheduled"
    )
    assert (child["spent_usd"], child["held_usd"], child["accounting_version"], child["enabled"]) == (
        "0.000000000", "0.000000000", 0, enabled
    )
    assert child["created_at"] == child["updated_at"] and child["created_at"] < child["effective_from"]
    admin_page: Final = await runtime.client.get(ROOT + "?limit=1&offset=1", headers={"X-Fixture-Actor": "admin"})
    assert admin_page.json()["items"][0]["successor_id"] == child["id"]
    own: Final = await runtime.client.get("/retail/model-budgets", headers={"X-Fixture-Actor": "owner"})
    rows: Final = own.json()["items"]
    assert (own.status_code, len(rows), rows[0]["predecessor_id"], rows[1]["successor_id"]) == (
        200, 2, parent["id"], child["id"]
    )
    assert not any(name in rows[0] for name in ("user_id", "created_by", "updated_by", "source"))
    async with runtime.store.diagnostic_transaction() as connection:
        event: Final = await (
            await connection.execute("SELECT action,resource_id,changes FROM retail_operation_events "
                                     "WHERE action='model_budget.scheduled'")
        ).fetchone()
    assert event is not None and event["resource_id"] == child["id"]
    assert event["changes"]["predecessor_id"] == parent["id"]
    assert datetime.fromisoformat(event["changes"]["start"]) == datetime.fromisoformat(str(parent["ends_at"]))


@pytest.mark.asyncio
async def test_concurrent_successor_retries_share_one_receipt_and_another_operation_cannot_add_a_second(
    runtime: Runtime,
) -> None:
    parent: Final = await _create(runtime)
    data: Final = ScheduleModelBudget.model_validate(_successor_body(runtime))
    replies: Final = await asyncio.gather(
        *(
            BudgetManagementStore(store).schedule("owner", _identifier(parent), data, ADMIN, "retry-successor")
            for store in (runtime.store, runtime.peer, runtime.store, runtime.peer)
        )
    )
    assert all(reply == replies[0] for reply in replies)
    changed_retry: Final = await runtime.client.post(
        ROOT + "/" + str(parent["id"]) + "/successor",
        json={**_successor_body(runtime), "limit_usd": "3"},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "retry-successor"},
    )
    assert changed_retry.status_code == 409
    duplicate: Final = await runtime.client.post(
        ROOT + "/" + str(parent["id"]) + "/successor",
        json=_successor_body(runtime),
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "second-child"},
    )
    assert duplicate.status_code == 409 and "no-store" in duplicate.headers["Cache-Control"]
    async with runtime.store.diagnostic_transaction() as connection:
        counts: Final = await (await connection.execute("""SELECT
          (SELECT count(*) FROM retail_model_budget_policies) AS policies,
          (SELECT count(*) FROM retail_operation_events) AS events,
          (SELECT count(*) FROM retail_model_budget_management_receipts) AS receipts""")).fetchone()
    assert counts == {"policies": 2, "events": 2, "receipts": 2}


@pytest.mark.asyncio
async def test_successor_rejects_stale_version_foreign_scope_and_advance_chaining(runtime: Runtime) -> None:
    parent: Final = await _create(runtime)
    root: Final = ROOT + "/" + str(parent["id"]) + "/successor"
    stale: Final = await runtime.client.post(
        root,
        json={**_successor_body(runtime), "expected_version": 2},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "stale"},
    )
    foreign: Final = await runtime.client.post(
        root.replace("owner", "other"),
        json=_successor_body(runtime),
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "foreign"},
    )
    assert stale.status_code == 409 and foreign.status_code == 404
    child: Final = await _schedule(runtime, parent)
    chain: Final = await runtime.client.post(
        ROOT + "/" + str(child["id"]) + "/successor",
        json={**_successor_body(runtime), "ends_at": (runtime.ends_at + timedelta(days=2)).isoformat()},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "chain"},
    )
    assert chain.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_days", (0, 367))
async def test_successor_end_must_follow_derived_start_and_fit_one_window(
    runtime: Runtime, extra_days: int
) -> None:
    parent: Final = await _create(runtime)
    response: Final = await runtime.client.post(
        ROOT + "/" + str(parent["id"]) + "/successor",
        json={**_successor_body(runtime), "ends_at": (runtime.ends_at + timedelta(days=extra_days)).isoformat()},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "bad-successor-end"},
    )
    assert response.status_code == 422
    async with runtime.store.diagnostic_transaction() as connection:
        count: Final = await (await connection.execute("SELECT count(*) AS total FROM retail_model_budget_policies")).fetchone()
    assert count == {"total": 1}


@pytest.mark.asyncio
async def test_successor_full_year_horizon_is_relative_to_its_start_not_creation_clock(runtime: Runtime) -> None:
    parent: Final = await _create(runtime)
    response: Final = await runtime.client.post(
        ROOT + "/" + str(parent["id"]) + "/successor",
        json={**_successor_body(runtime), "ends_at": (runtime.ends_at + timedelta(days=366)).isoformat()},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "full-year-successor"},
    )
    assert response.status_code == 201, response.text
    assert datetime.fromisoformat(response.json()["ends_at"]) - datetime.fromisoformat(
        response.json()["effective_from"]
    ) == timedelta(days=366)


@pytest.mark.asyncio
async def test_scheduled_configuration_can_change_without_retiming_or_changing_parent_usage(runtime: Runtime) -> None:
    parent: Final = await _create(runtime)
    await runtime.store.reserve("owner", "old-reserve", "public-model", "0.25", {})
    child: Final = await _schedule(runtime, parent)
    changed: Final = await runtime.client.patch(
        ROOT + "/" + str(child["id"]),
        json={"limit_usd": "0.5", "enabled": False, "expected_version": 1},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "update-scheduled"},
    )
    assert changed.status_code == 200, changed.text
    assert (changed.json()["state"], changed.json()["enabled"], changed.json()["predecessor_id"]) == (
        "scheduled", False, parent["id"]
    )
    parent_change: Final = await runtime.client.patch(
        ROOT + "/" + str(parent["id"]),
        json={"limit_usd": "3", "expected_version": 1},
        headers={"X-Fixture-Actor": "admin", "Idempotency-Key": "update-parent"},
    )
    assert parent_change.status_code == 200 and parent_change.json()["successor_id"] == child["id"]
    assert parent_change.json()["held_usd"] == "0.250000000"
    assert changed.json()["effective_from"] == child["effective_from"] and changed.json()["ends_at"] == child["ends_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ("owner", "viewer"))
async def test_successor_management_requires_writable_native_admin(runtime: Runtime, role: str) -> None:
    parent: Final = await _create(runtime)
    denied: Final = await runtime.client.post(
        ROOT + "/" + str(parent["id"]) + "/successor",
        json=_successor_body(runtime),
        headers={"X-Fixture-Actor": role, "Idempotency-Key": "not-admin-successor"},
    )
    assert denied.status_code == 403 and "no-store" in denied.headers["Cache-Control"]


@pytest.mark.asyncio
async def test_successor_replays_original_receipt_after_parent_expiry_but_rejects_new_operation(runtime: Runtime) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        clock: Final = await (await connection.execute("SELECT clock_timestamp() AS at")).fetchone()
    assert clock is not None
    end: Final = clock["at"] + timedelta(seconds=0.6)
    parent: Final = await BudgetManagementStore(runtime.store).create(
        "owner", CreateModelBudget(model="public-model", ends_at=end, limit_usd="1"), ADMIN, "short-parent"
    )
    data: Final = ScheduleModelBudget(expected_version=1, ends_at=runtime.ends_at, limit_usd="2")
    child: Final = await BudgetManagementStore(runtime.store).schedule("owner", _identifier(parent), data, ADMIN, "next")
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute(
            "SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM (%s::timestamptz-clock_timestamp())))+0.03)", (end,)
        )
    replay: Final = await BudgetManagementStore(runtime.peer).schedule("owner", _identifier(parent), data, ADMIN, "next")
    assert replay == child and replay["state"] == "scheduled"
    with pytest.raises(HTTPException) as expired:
        await BudgetManagementStore(runtime.peer).schedule("owner", _identifier(parent), data, ADMIN, "new-next")
    assert expired.value.status_code == 409
    page: Final = await BudgetManagementStore(runtime.store).list("owner", OWN, admin=False)
    assert tuple(row["state"] for row in _ITEMS.validate_python(page["items"])) == ("active", "expired")


@pytest.mark.asyncio
async def test_successor_waiting_on_idempotency_lock_cannot_backfill_after_parent_expiry(runtime: Runtime) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        clock: Final = await (await connection.execute("SELECT clock_timestamp() AS at")).fetchone()
    assert clock is not None
    end: Final = clock["at"] + timedelta(seconds=0.6)
    parent: Final = await BudgetManagementStore(runtime.store).create(
        "owner", CreateModelBudget(model="public-model", ends_at=end, limit_usd="1"), ADMIN, "short-parent"
    )
    async with runtime.store.diagnostic_transaction() as blocker:
        await blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("retail:operation:admin:late-successor",)
        )
        pending: Final = asyncio.create_task(
            BudgetManagementStore(runtime.peer).schedule(
                "owner",
                _identifier(parent),
                ScheduleModelBudget(expected_version=1, ends_at=runtime.ends_at, limit_usd="2"),
                ADMIN,
                "late-successor",
            )
        )
        try:
            for _ in range(100):
                waiting: Final = await (
                    await blocker.execute("""SELECT count(*) AS total FROM pg_locks
                    WHERE locktype='advisory' AND NOT granted
                    AND database=(SELECT oid FROM pg_database WHERE datname=current_database())""")
                ).fetchone()
                if waiting is not None and waiting["total"] > 0:
                    break
                await asyncio.sleep(0.002)
            else:
                pytest.fail("The successor never reached the held idempotency lock")
            await blocker.execute(
                "SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM (%s::timestamptz-clock_timestamp())))+0.03)", (end,)
            )
        except BaseException:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            raise
    with pytest.raises(HTTPException) as expired:
        await pending
    assert expired.value.status_code == 409
    async with runtime.store.diagnostic_transaction() as connection:
        counts: Final = await (await connection.execute("""SELECT
          (SELECT count(*) FROM retail_model_budget_policies) AS policies,
          (SELECT count(*) FROM retail_operation_events) AS events,
          (SELECT count(*) FROM retail_model_budget_management_receipts) AS receipts""")).fetchone()
    assert counts == {"policies": 1, "events": 1, "receipts": 1}


@pytest.mark.asyncio
async def test_successor_requires_parent_public_model_to_remain_configured(runtime: Runtime) -> None:
    parent: Final = await _create(runtime)
    with pytest.raises(HTTPException) as missing:
        await BudgetManagementStore(runtime.store, models=tuple).schedule(
            "owner", _identifier(parent), ScheduleModelBudget.model_validate(_successor_body(runtime)), ADMIN, "removed"
        )
    assert missing.value.status_code == 422
