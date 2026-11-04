import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import JsonValue

from litellm.proxy.retail.budget_accounting import initialize_schema
from litellm.proxy.retail.budget_management import BudgetManagementStore, ScheduleModelBudget, UpdateModelBudget
from litellm.proxy.retail.control_plane_access import ControlPlaneActor
from litellm.proxy.retail.models import BudgetExceeded, IdempotencyConflict
from litellm.proxy.retail.monthly_budget_store import (
    CreateMonthlyBudget,
    MonthlyBudgetStore,
    StopMonthlyBudget,
    UpdateMonthlyBudget,
    following_month,
    month_floor,
)
from litellm.proxy.retail.monthly_budget_worker import MonthlyBudgetWorker
from litellm.proxy.retail.store import WalletStore

ADMIN: Final = ControlPlaneActor("admin", "proxy_admin", True, True)
VIEWER: Final = ControlPlaneActor("admin", "proxy_admin_viewer", True, False)


def models() -> tuple[str, ...]:
    return ("public-model", "other-model")


@dataclass(frozen=True, slots=True)
class Runtime:
    wallet: WalletStore
    peer: WalletStore
    monthly: MonthlyBudgetStore
    manual: BudgetManagementStore


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    address: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not address:
        pytest.skip("A dedicated monthly budget PostgreSQL service is required")
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_MODEL_BUDGETS_ENABLED", "true")
    monkeypatch.setenv("RETAIL_MONTHLY_BUDGETS_ENABLED", "true")
    base: Final = conninfo_to_dict(address)
    name: Final = "monthly_budget_" + uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    selected: Final = make_conninfo(**{**base, "dbname": name})
    wallet: Final = WalletStore(selected, max_connections=4, diagnostic_connections=3, critical_connections=2)
    peer: Final = WalletStore(selected, max_connections=4, diagnostic_connections=3, critical_connections=2)
    try:
        async with await psycopg.AsyncConnection.connect(selected) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        await wallet.initialize()
        await wallet.initialize_control_plane()
        await peer.initialize()
        for user in ("account", "other-account"):
            await wallet.get_wallet(user, create=True)
            await wallet.credit(user, "1000", "fixture-" + user, "fixture", ADMIN.user_id, "seed-" + user)
        yield Runtime(wallet, peer, MonthlyBudgetStore(wallet, models=models), BudgetManagementStore(wallet, models=models))
    finally:
        await asyncio.gather(wallet.aclose(), peer.aclose())
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def next_data() -> CreateMonthlyBudget:
    return CreateMonthlyBudget(model="public-model", start_month=following_month(datetime.now(timezone.utc), "UTC").strftime("%Y-%m"),
                               timezone="UTC", limit_usd="1")


async def seed_elapsed_plan(runtime: Runtime, *, months_ago: bool = True) -> UUID:
    current: Final = month_floor(datetime.now(timezone.utc), "UTC")
    start: Final = month_floor(current - timedelta(days=1), "UTC") if months_ago else current
    identifier: Final = uuid4()
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute("SELECT user_id FROM retail_wallets WHERE user_id='account' FOR UPDATE")
        await connection.execute("""INSERT INTO retail_model_budget_recurrences
            (id,user_id,model,timezone,starts_at,created_by,updated_by) VALUES(%s,'account','public-model','UTC',%s,'admin','admin')""",
            (identifier, start))
        await connection.execute("INSERT INTO retail_model_budget_recurrence_terms VALUES(%s,%s,1000000000)", (identifier, start))
    return identifier


async def policy_rows(runtime: Runtime) -> tuple[Mapping[str, JsonValue], ...]:
    async with runtime.wallet.diagnostic_transaction() as connection:
        return tuple(await (await connection.execute("SELECT * FROM retail_model_budget_policies ORDER BY effective_from,id")).fetchall())


@pytest.mark.asyncio
async def test_creation_is_next_full_month_with_atomic_idempotent_audit_and_strict_scope(runtime: Runtime) -> None:
    data: Final = next_data()
    response: Final = await runtime.monthly.create("account", data, ADMIN, "create")
    assert response == await runtime.monthly.create("account", data, ADMIN, "create")
    assert response["state"] == "scheduled" and response["current_window_ready"] is False
    rows: Final = await policy_rows(runtime)
    assert len(rows) == 1 and rows[0]["effective_from"] == following_month(datetime.now(timezone.utc), "UTC")
    assert rows[0]["spent_nanos"] == rows[0]["held_nanos"] == 0
    with pytest.raises(IdempotencyConflict):
        await runtime.monthly.create("account", data.model_copy(update={"limit_usd": "2.000000000"}), ADMIN, "create")
    with pytest.raises(HTTPException) as denied:
        await runtime.monthly.create("other-account", data, VIEWER, "viewer")
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as duplicate:
        await runtime.monthly.create("account", data, ADMIN, "duplicate")
    assert duplicate.value.status_code == 409
    async with runtime.wallet.diagnostic_transaction() as connection:
        events: Final = await (await connection.execute("SELECT action FROM retail_operation_events")).fetchall()
    assert events == [{"action": "monthly_budget.created"}]


@pytest.mark.asyncio
async def test_creation_rejects_partial_current_month_and_unconfigured_model(runtime: Runtime) -> None:
    data: Final = next_data()
    with pytest.raises(HTTPException) as partial:
        await runtime.monthly.create("account", data.model_copy(update={"start_month": datetime.now(timezone.utc).strftime("%Y-%m")}), ADMIN, "partial")
    assert partial.value.status_code == 422
    with pytest.raises(HTTPException) as unknown:
        await runtime.monthly.create("account", data.model_copy(update={"model": "not-configured"}), ADMIN, "unknown")
    assert unknown.value.status_code == 422
    assert await policy_rows(runtime) == ()


@pytest.mark.asyncio
async def test_missing_occurrence_is_fail_closed_even_when_worker_feature_is_disabled(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed_elapsed_plan(runtime)
    monkeypatch.setenv("RETAIL_MONTHLY_BUDGETS_ENABLED", "false")
    assert await MonthlyBudgetWorker(runtime.monthly).run_once() == 0
    before: Final = await runtime.wallet.get_wallet("account")
    with pytest.raises(BudgetExceeded):
        await runtime.wallet.reserve("account", "missing", "public-model", "0.5", {})
    assert await runtime.wallet.get_reservation("missing") is None
    after: Final = await runtime.wallet.get_wallet("account")
    assert before == after
    assert await runtime.wallet.reserve("other-account", "other-user", "public-model", "0.5", {})
    assert await runtime.wallet.reserve("account", "other-model", "other-model", "0.5", {})


@pytest.mark.asyncio
async def test_offline_recovery_only_materializes_current_and_next_not_historical_months(runtime: Runtime) -> None:
    await seed_elapsed_plan(runtime)
    assert await runtime.monthly.materialize_batch() == 1
    assert await runtime.monthly.materialize_batch() == 0
    rows: Final = await policy_rows(runtime)
    current: Final = month_floor(datetime.now(timezone.utc), "UTC")
    assert len(rows) == 2 and tuple(row["effective_from"] for row in rows) == (current, following_month(current, "UTC"))
    assert await runtime.wallet.reserve("account", "admitted", "public-model", "0.75", {})
    with pytest.raises(BudgetExceeded):
        await runtime.wallet.reserve("account", "over-budget", "public-model", "0.5", {})
    await runtime.wallet.settle("admitted", "0.5", {})
    current_row: Final = (await policy_rows(runtime))[0]
    assert current_row["spent_nanos"] == 500_000_000 and current_row["held_nanos"] == 0


@pytest.mark.asyncio
async def test_future_amount_revision_cannot_reprice_missing_current_or_existing_admissions(runtime: Runtime) -> None:
    identifier: Final = await seed_elapsed_plan(runtime)
    revised: Final = await runtime.monthly.update("account", identifier, UpdateMonthlyBudget(expected_version=1, limit_usd="2"), ADMIN, "raise")
    assert revised["version"] == 2 and revised["limit_usd"] == "2.000000000"
    rows: Final = await policy_rows(runtime)
    assert tuple(row["limit_nanos"] for row in rows) == (1_000_000_000, 2_000_000_000)
    await runtime.wallet.reserve("account", "old-admission", "public-model", "0.8", {})
    await runtime.wallet.release("old-admission", "fixture-released")
    await runtime.monthly.update("account", identifier, UpdateMonthlyBudget(expected_version=2, limit_usd="3"), ADMIN, "raise-again")
    await runtime.wallet.settle("old-admission", "0.6", {})
    updated: Final = await policy_rows(runtime)
    assert updated[0]["limit_nanos"] == 1_000_000_000 and updated[0]["spent_nanos"] == 600_000_000
    assert updated[1]["limit_nanos"] == 3_000_000_000


@pytest.mark.asyncio
async def test_stop_preserves_current_enforcement_and_cancels_only_unstarted_window(runtime: Runtime) -> None:
    identifier: Final = await seed_elapsed_plan(runtime)
    await runtime.monthly.materialize_batch()
    stopped: Final = await runtime.monthly.stop("account", identifier, StopMonthlyBudget(expected_version=1), ADMIN, "stop")
    assert stopped["state"] == "stopping"
    rows: Final = await policy_rows(runtime)
    assert rows[0]["enabled"] is True and rows[1]["enabled"] is False
    with pytest.raises(BudgetExceeded):
        await runtime.wallet.reserve("account", "still-capped", "public-model", "1.5", {})
    assert stopped == await runtime.monthly.stop("account", identifier, StopMonthlyBudget(expected_version=1), ADMIN, "stop")
    replacement: Final = await runtime.monthly.create("account", next_data().model_copy(update={"limit_usd": "2.000000000"}), ADMIN, "replacement")
    assert replacement["state"] == "scheduled"
    assert len(await policy_rows(runtime)) == 3


@pytest.mark.asyncio
async def test_manual_endpoints_cannot_edit_or_schedule_monthly_child_and_database_freezes_active_configuration(runtime: Runtime) -> None:
    await seed_elapsed_plan(runtime)
    await runtime.monthly.materialize_batch()
    rows: Final = await policy_rows(runtime)
    identifier: Final = UUID(str(rows[0]["id"]))
    with pytest.raises(HTTPException) as edited:
        await runtime.manual.update("account", identifier, UpdateModelBudget(expected_version=1, enabled=False), ADMIN, "manual-edit")
    assert edited.value.status_code == 409
    with pytest.raises(HTTPException) as scheduled:
        await runtime.manual.schedule("account", identifier, ScheduleModelBudget(expected_version=1, limit_usd="2",
            ends_at=datetime.now(timezone.utc) + timedelta(days=100)), ADMIN, "manual-next")
    assert scheduled.value.status_code == 409
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        async with runtime.wallet.diagnostic_transaction() as connection:
            await connection.execute("UPDATE retail_model_budget_policies SET enabled=false WHERE id=%s", (identifier,))
    own: Final = ControlPlaneActor("account", "internal_user", False, True)
    listing: Final = await runtime.manual.list("account", own, admin=False)
    assert str((await runtime.monthly.list("account", own, admin=False))["items"][0]["id"]) in str(listing["items"])


@pytest.mark.asyncio
async def test_legacy_reservation_insert_stays_fail_closed_after_old_budget_schema_reload(runtime: Runtime) -> None:
    await seed_elapsed_plan(runtime)
    schema: Final = Path(__file__).parents[3] / "litellm/proxy/retail/budget_schema.sql"
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute(schema.read_text(), prepare=False)
    with pytest.raises(psycopg.errors.CheckViolation):
        async with runtime.wallet.diagnostic_transaction() as connection:
            await connection.execute("UPDATE retail_wallets SET reserved_nanos=reserved_nanos+1 WHERE user_id='account'")
            await connection.execute("""INSERT INTO retail_reservations(request_id,user_id,model,reserved_nanos,price_snapshot)
                VALUES('old-runtime','account','public-model',1,'{}')""")
    assert await runtime.wallet.get_reservation("old-runtime") is None
    async with runtime.wallet.diagnostic_transaction() as connection:
        row: Final = await (await connection.execute("SELECT reserved_nanos FROM retail_wallets WHERE user_id='account'")).fetchone()
    assert row == {"reserved_nanos": 0}


@pytest.mark.asyncio
async def test_concurrent_workers_and_stale_management_cas_do_not_duplicate_or_reset_occurrences(runtime: Runtime) -> None:
    identifier: Final = await seed_elapsed_plan(runtime)
    peer: Final = MonthlyBudgetStore(runtime.peer, models=models)
    await asyncio.gather(runtime.monthly.materialize_batch(), peer.materialize_batch())
    assert len(await policy_rows(runtime)) == 2
    responses: Final = await asyncio.gather(
        runtime.monthly.update("account", identifier, UpdateMonthlyBudget(expected_version=1, limit_usd="2"), ADMIN, "first"),
        peer.update("account", identifier, UpdateMonthlyBudget(expected_version=1, limit_usd="3"), ADMIN, "second"),
        return_exceptions=True,
    )
    assert sum(isinstance(response, HTTPException) and response.status_code == 409 for response in responses) == 1
    assert len(await policy_rows(runtime)) == 2


@pytest.mark.asyncio
async def test_self_projection_hides_admin_metadata_and_other_accounts_are_unavailable(runtime: Runtime) -> None:
    response: Final = await runtime.monthly.create("account", next_data(), ADMIN, "create")
    own: Final = ControlPlaneActor("account", "internal_user", False, True)
    projection: Final = await runtime.monthly.get("account", UUID(str(response["id"])), own, admin=False)
    assert "created_by" not in projection and "updated_by" not in projection and "user_id" not in projection
    with pytest.raises(HTTPException) as denied:
        await runtime.monthly.list("other-account", own, admin=False)
    assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_shanghai_plan_uses_local_month_start_not_utc_month_start(runtime: Runtime) -> None:
    first: Final = following_month(datetime.now(timezone.utc), "Asia/Shanghai")
    data: Final = CreateMonthlyBudget(model="public-model", start_month=first.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m"),
                                     timezone="Asia/Shanghai", limit_usd="0.000000001")
    await runtime.monthly.create("account", data, ADMIN, "shanghai")
    rows: Final = await policy_rows(runtime)
    assert rows[0]["effective_from"] == first and rows[0]["limit_nanos"] == 1


@pytest.mark.asyncio
async def test_stop_before_first_month_is_explicit_and_does_not_poison_replacement_scope(runtime: Runtime) -> None:
    created: Final = await runtime.monthly.create("account", next_data(), ADMIN, "create")
    stopped: Final = await runtime.monthly.stop("account", UUID(str(created["id"])), StopMonthlyBudget(expected_version=1), ADMIN, "stop")
    assert stopped["state"] == "stopped" and stopped["starts_at"] == stopped["stops_at"]
    assert (await policy_rows(runtime))[0]["enabled"] is False
    await runtime.monthly.create("account", next_data(), ADMIN, "replacement")
    assert len(await policy_rows(runtime)) == 2
    assert await runtime.wallet.reserve("account", "before-first-month", "public-model", "10", {})


@pytest.mark.asyncio
async def test_audit_outage_rolls_back_plan_terms_occurrences_and_retry_is_atomic(runtime: Runtime) -> None:
    async with await psycopg.AsyncConnection.connect(runtime.wallet.database_url) as lock:
        await lock.execute("LOCK TABLE retail_operation_events IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await runtime.monthly.create("account", next_data(), ADMIN, "retry-after-outage")
    assert await policy_rows(runtime) == ()
    assert (await runtime.monthly.list("account", ADMIN))["total"] == 0
    assert (await runtime.monthly.create("account", next_data(), ADMIN, "retry-after-outage"))["version"] == 1


@pytest.mark.asyncio
async def test_frozen_monthly_configuration_survives_legacy_schema_reload(runtime: Runtime) -> None:
    await seed_elapsed_plan(runtime)
    await runtime.monthly.materialize_batch()
    current: Final = (await policy_rows(runtime))[0]
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute((Path(__file__).parents[3] / "litellm/proxy/retail/budget_schema.sql").read_text(), prepare=False)
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        async with runtime.wallet.diagnostic_transaction() as connection:
            await connection.execute("UPDATE retail_model_budget_policies SET limit_nanos=limit_nanos+1 WHERE id=%s", (current["id"],))
    await runtime.wallet.reserve("account", "legacy-admission", "public-model", "0.5", {})
    await runtime.wallet.settle("legacy-admission", "0.4", {})
    assert (await policy_rows(runtime))[0]["spent_nanos"] == 400_000_000


@pytest.mark.asyncio
async def test_mixed_old_policy_guard_cannot_silently_shorten_monthly_window(runtime: Runtime) -> None:
    await seed_elapsed_plan(runtime)
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute((Path(__file__).parents[3] / "litellm/proxy/retail/budget_schema.sql").read_text(), prepare=False)
    assert await runtime.monthly.materialize_batch() == 1
    assert await policy_rows(runtime) == ()
    with pytest.raises(BudgetExceeded):
        await runtime.wallet.reserve("account", "mixed-runtime", "public-model", "0.5", {})
    async with runtime.wallet.diagnostic_transaction() as connection:
        await initialize_schema(connection)
        await connection.execute("UPDATE retail_model_budget_recurrences SET reconcile_after=clock_timestamp()")
    await runtime.monthly.materialize_batch()
    assert len(await policy_rows(runtime)) == 2
    assert await runtime.wallet.reserve("account", "recovered-runtime", "public-model", "0.5", {})


@pytest.mark.asyncio
async def test_busy_wallet_is_skipped_without_reporting_work_or_hammering_the_worker_loop(runtime: Runtime) -> None:
    await seed_elapsed_plan(runtime)
    async with await psycopg.AsyncConnection.connect(runtime.wallet.database_url) as lock:
        await lock.execute("SELECT user_id FROM retail_wallets WHERE user_id='account' FOR UPDATE")
        assert await MonthlyBudgetWorker(runtime.monthly).run_once() == 0
        assert await policy_rows(runtime) == ()
    assert await MonthlyBudgetWorker(runtime.monthly).run_once() == 1
    assert len(await policy_rows(runtime)) == 2
