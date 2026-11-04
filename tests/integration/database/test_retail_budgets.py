import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import JsonValue

from litellm.proxy.retail.budget_accounting import BudgetPolicyRow, lock_budget_wallet
from litellm.proxy.retail.models import BudgetExceeded, IdempotencyConflict
from litellm.proxy.retail.settlement_store import claim_facts, record_facts
from litellm.proxy.retail.settlement_worker import SettlementWorker
from litellm.proxy.retail.store import WalletStore


@dataclass(frozen=True, slots=True)
class BudgetRuntime:
    store: WalletStore
    peer: WalletStore


@pytest_asyncio.fixture(loop_scope="function")
async def runtime() -> AsyncIterator[BudgetRuntime]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated budget-test PostgreSQL service is required")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_budget_" + uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    address: Final = make_conninfo(**{**base, "dbname": name})
    store: Final = WalletStore(address, max_connections=4, diagnostic_connections=2, critical_connections=2)
    peer: Final = WalletStore(address, max_connections=4, diagnostic_connections=2, critical_connections=2)
    try:
        async with await psycopg.AsyncConnection.connect(address) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        await store.initialize()
        await peer.initialize()
        for user in ("account", "other-account"):
            await store.get_wallet(user, create=True)
            await store.credit(user, "1000", "seed-" + user, "fixture", "fixture", "seed-" + user)
        yield BudgetRuntime(store, peer)
    finally:
        await asyncio.gather(store.aclose(), peer.aclose())
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def _budget(
    runtime: BudgetRuntime,
    *,
    limit: int = 1000000000,
    enabled: bool = True,
    user_id: str = "account",
    model: str = "public-model",
    seconds: float = 3600,
) -> BudgetPolicyRow:
    async with runtime.store.diagnostic_transaction() as connection:
        await lock_budget_wallet(connection, user_id)
        row: Final = await (
            await connection.execute(
                """INSERT INTO retail_model_budget_policies(id,user_id,model,ends_at,limit_nanos,enabled,created_by,updated_by)
            VALUES(%s,%s,%s,clock_timestamp()+(%s*interval '1 second'),%s,%s,'admin','admin') RETURNING *""",
                (uuid4(), user_id, model, seconds, limit, enabled),
            )
        ).fetchone()
    assert row is not None
    return BudgetPolicyRow.model_validate(row)


async def _policy(runtime: BudgetRuntime, identifier: UUID) -> BudgetPolicyRow:
    async with runtime.store.diagnostic_transaction() as connection:
        row: Final = await (
            await connection.execute("SELECT * FROM retail_model_budget_policies WHERE id=%s", (identifier,))
        ).fetchone()
    assert row is not None
    return BudgetPolicyRow.model_validate(row)


async def _reserve(
    store: WalletStore,
    request_id: str,
    amount: str = "0.4",
    *,
    user_id: str = "account",
    model: str = "public-model",
    snapshot: Mapping[str, JsonValue] | None = None,
) -> Mapping[str, JsonValue]:
    return await store.reserve(user_id, request_id, model, amount, snapshot or {}, max_parallel=1000)


async def _configure(runtime: BudgetRuntime, identifier: UUID, *, enabled: bool, limit: int) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await lock_budget_wallet(connection, "account")
        await connection.execute(
            """UPDATE retail_model_budget_policies SET enabled=%s,limit_nanos=%s,config_version=config_version+1,
            updated_at=clock_timestamp(),updated_by='admin' WHERE id=%s""",
            (enabled, limit, identifier),
        )


@pytest.mark.asyncio
async def test_concurrent_multi_store_admission_cannot_oversubscribe_a_budget(runtime: BudgetRuntime) -> None:
    budget: Final = await _budget(runtime)
    results: Final = await asyncio.gather(
        *(_reserve(runtime.store if index % 2 else runtime.peer, "request-" + str(index)) for index in range(8)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Mapping) for result in results) == 2
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 6
    current: Final = await _policy(runtime, budget.id)
    assert current.held_nanos == 800000000 and current.spent_nanos == 0 and current.accounting_version == 2
    assert (await runtime.store.get_wallet("account"))["reserved"] == "0.800000000"


@pytest.mark.asyncio
async def test_duplicate_reserve_and_terminal_callbacks_adjust_counters_once(runtime: BudgetRuntime) -> None:
    budget: Final = await _budget(runtime)
    reserved: Final = await asyncio.gather(*(_reserve(runtime.peer, "same") for _ in range(6)))
    assert all(row == reserved[0] for row in reserved)
    settled: Final = await asyncio.gather(*(runtime.store.settle("same", "0.3", {"usage": "same"}) for _ in range(6)))
    assert all(row == settled[0] for row in settled)
    await runtime.store.release("same", "late failure")
    current: Final = await _policy(runtime, budget.id)
    assert current.held_nanos == 0 and current.spent_nanos == 300000000 and current.accounting_version == 2
    assert current.config_version == 1 and current.updated_at == budget.updated_at
    with pytest.raises(IdempotencyConflict):
        await _reserve(runtime.peer, "same", "0.5")
    with pytest.raises(IdempotencyConflict):
        await runtime.store.settle("same", "0.2", {})


@pytest.mark.asyncio
async def test_ordinary_release_then_late_known_usage_charges_original_window_once(runtime: BudgetRuntime) -> None:
    budget: Final = await _budget(runtime)
    await _reserve(runtime.store, "late")
    await asyncio.gather(*(runtime.store.release("late", "upstream rejection") for _ in range(4)))
    released: Final = await _policy(runtime, budget.id)
    assert released.held_nanos == 0 and released.spent_nanos == 0 and released.accounting_version == 2
    await record_facts(runtime.store, "late", "0.25", {"usage": "late"})
    await SettlementWorker(runtime.store).run_once()
    current: Final = await _policy(runtime, budget.id)
    assert current.spent_nanos == 250000000 and current.held_nanos == 0 and current.accounting_version == 3


@pytest.mark.asyncio
async def test_manual_release_prevents_late_usage_from_changing_budget_or_wallet(runtime: BudgetRuntime) -> None:
    budget: Final = await _budget(runtime)
    await _reserve(runtime.store, "manual")
    details: Final = {"actor_id": "admin", "reason": "reviewed", "reference": "manual", "idempotency_key": "manual"}
    await runtime.store.release("manual", "reviewed", details)
    await record_facts(runtime.store, "manual", "0.2", {"usage": "late"})
    await SettlementWorker(runtime.store).run_once()
    current: Final = await _policy(runtime, budget.id)
    assert current.spent_nanos == 0 and current.held_nanos == 0
    assert (await runtime.store.get_wallet("account"))["balance"] == "1000.000000000"
    async with runtime.store.critical_transaction() as connection:
        fact: Final = await (
            await connection.execute("SELECT state FROM retail_settlement_facts WHERE request_id='manual'")
        ).fetchone()
    assert fact == {"state": "conflict"}


@pytest.mark.asyncio
async def test_unknown_and_pending_facts_keep_budget_hold_after_activity_lease_expires(runtime: BudgetRuntime) -> None:
    budget: Final = await _budget(runtime)
    await _reserve(runtime.store, "unknown")
    await record_facts(runtime.store, "unknown", None, {"billing_error": "missing"})
    async with runtime.store.transaction() as connection:
        await connection.execute(
            "UPDATE retail_reservations SET active_until=now()-interval '1 second' WHERE request_id='unknown'"
        )
    assert await SettlementWorker(runtime.store).run_once() == 0
    unknown: Final = await _policy(runtime, budget.id)
    assert unknown.held_nanos == 400000000 and unknown.spent_nanos == 0
    await record_facts(runtime.store, "unknown", "0.2", {"usage": "known"})
    await claim_facts(runtime.store, "paused-worker", 1, 60)
    pending: Final = await _policy(runtime, budget.id)
    assert pending.held_nanos == unknown.held_nanos and pending.spent_nanos == 0


@pytest.mark.asyncio
async def test_actual_over_quote_is_recorded_in_full_and_blocks_later_admission(runtime: BudgetRuntime) -> None:
    budget: Final = await _budget(runtime, limit=500000000)
    await _reserve(runtime.store, "overrun", "0.2")
    await runtime.store.settle("overrun", "0.8", {})
    current: Final = await _policy(runtime, budget.id)
    assert current.spent_nanos == 800000000 and current.held_nanos == 0
    assert (await runtime.store.get_wallet("account"))["balance"] == "999.200000000"
    with pytest.raises(BudgetExceeded):
        await _reserve(runtime.peer, "next", "0.01")


@pytest.mark.asyncio
async def test_disabled_policy_tracks_usage_and_reenable_never_resets_counters(
    runtime: BudgetRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget: Final = await _budget(runtime, limit=100000000, enabled=False)
    monkeypatch.setenv("RETAIL_MODEL_BUDGETS_ENABLED", "false")
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "false")
    await _reserve(runtime.store, "disabled")
    await _configure(runtime, budget.id, enabled=True, limit=100000000)
    with pytest.raises(BudgetExceeded):
        await _reserve(runtime.peer, "resumed", "0.01")
    await runtime.store.settle("disabled", "0.3", {})
    current: Final = await _policy(runtime, budget.id)
    assert current.spent_nanos == 300000000 and current.held_nanos == 0 and current.config_version == 2
    await _configure(runtime, budget.id, enabled=False, limit=100000000)
    await _reserve(runtime.peer, "disabled-again", "0.01")
    assert (await _policy(runtime, budget.id)).held_nanos == 10000000


@pytest.mark.asyncio
async def test_lowering_or_raising_limit_only_changes_future_admission(runtime: BudgetRuntime) -> None:
    budget: Final = await _budget(runtime)
    await _reserve(runtime.store, "inflight")
    await _configure(runtime, budget.id, enabled=True, limit=200000000)
    with pytest.raises(BudgetExceeded):
        await _reserve(runtime.peer, "lowered", "0.01")
    await runtime.store.settle("inflight", "0.3", {})
    await _configure(runtime, budget.id, enabled=True, limit=1000000000)
    await _reserve(runtime.peer, "raised", "0.4")
    current: Final = await _policy(runtime, budget.id)
    assert current.spent_nanos == 300000000 and current.held_nanos == 400000000 and current.config_version == 3


@pytest.mark.asyncio
async def test_request_reserved_before_policy_creation_is_never_retroactively_counted(runtime: BudgetRuntime) -> None:
    await _reserve(runtime.store, "before")
    budget: Final = await _budget(runtime)
    await runtime.store.settle("before", "0.3", {})
    current: Final = await _policy(runtime, budget.id)
    assert current.spent_nanos == 0 and current.held_nanos == 0 and current.accounting_version == 0
    await _reserve(runtime.peer, "after")
    assert (await _policy(runtime, budget.id)).held_nanos == 400000000


@pytest.mark.asyncio
async def test_expired_policy_keeps_old_hold_and_next_window_starts_independent_counters(
    runtime: BudgetRuntime,
) -> None:
    first: Final = await _budget(runtime, seconds=0.3)
    await _reserve(runtime.store, "old")
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("SELECT pg_sleep(0.35)")
    second: Final = await _budget(runtime, limit=100000000)
    await _reserve(runtime.peer, "new", "0.05")
    await runtime.store.settle("old", "0.3", {})
    previous: Final = await _policy(runtime, first.id)
    current: Final = await _policy(runtime, second.id)
    assert previous.spent_nanos == 300000000 and previous.held_nanos == 0
    assert current.spent_nanos == 0 and current.held_nanos == 50000000


@pytest.mark.asyncio
async def test_disabled_windows_still_reject_overlap_and_window_identity_is_immutable(runtime: BudgetRuntime) -> None:
    budget: Final = await _budget(runtime, enabled=False)
    with pytest.raises(psycopg.errors.ExclusionViolation):
        await _budget(runtime)
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        async with runtime.store.diagnostic_transaction() as connection:
            await connection.execute(
                "UPDATE retail_model_budget_policies SET ends_at=ends_at+interval '1 day' WHERE id=%s", (budget.id,)
            )
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        async with runtime.store.diagnostic_transaction() as connection:
            await connection.execute("DELETE FROM retail_model_budget_policies WHERE id=%s", (budget.id,))


@pytest.mark.asyncio
async def test_budget_scopes_are_exact_user_and_public_model_not_pool_or_key(runtime: BudgetRuntime) -> None:
    budget: Final = await _budget(runtime, limit=500000000)
    await _reserve(runtime.store, "pool-a", "0.3", snapshot={"resource_pool_id": "pool-a", "credential": "key-a"})
    with pytest.raises(BudgetExceeded):
        await _reserve(runtime.peer, "pool-b", "0.3", snapshot={"resource_pool_id": "pool-b", "credential": "key-b"})
    await _reserve(runtime.peer, "other-model", "0.9", model="other-model")
    await _reserve(runtime.peer, "other-user", "0.9", user_id="other-account")
    assert (await _policy(runtime, budget.id)).held_nanos == 300000000


@pytest.mark.asyncio
async def test_settlement_ledger_failure_rolls_back_budget_wallet_and_allocation_together(
    runtime: BudgetRuntime,
) -> None:
    budget: Final = await _budget(runtime)
    await _reserve(runtime.store, "rollback")
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute(
            """CREATE FUNCTION reject_budget_charge() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF NEW.kind='charge' THEN RAISE EXCEPTION 'fixture charge failure'; END IF; RETURN NEW; END $$;
          CREATE TRIGGER reject_budget_charge BEFORE INSERT ON retail_ledger
          FOR EACH ROW EXECUTE FUNCTION reject_budget_charge();""",
            prepare=False,
        )
    with pytest.raises(psycopg.errors.RaiseException):
        await runtime.store.settle("rollback", "0.3", {})
    current: Final = await _policy(runtime, budget.id)
    assert current.spent_nanos == 0 and current.held_nanos == 400000000 and current.accounting_version == 1
    assert (await runtime.store.get_wallet("account"))["reserved"] == "0.400000000"
    assert (await runtime.store.get_reservation("rollback"))["status"] == "reserved"


@pytest.mark.asyncio
async def test_terminal_facts_keep_critical_capacity_when_finance_pool_is_saturated(runtime: BudgetRuntime) -> None:
    budget: Final = await _budget(runtime)
    await _reserve(runtime.store, "isolated")
    async with AsyncExitStack() as occupied:
        for _ in range(4):
            connection: Final = await occupied.enter_async_context(runtime.store.transaction())
            await connection.execute("SELECT 1")
        await asyncio.wait_for(record_facts(runtime.store, "isolated", "0.2", {"usage": "known"}), timeout=2)
    assert (await _policy(runtime, budget.id)).held_nanos == 400000000
    assert await SettlementWorker(runtime.store).run_once() == 1
    assert (await _policy(runtime, budget.id)).spent_nanos == 200000000


async def _wallet_waiter(connection: psycopg.AsyncConnection) -> None:
    async with asyncio.timeout(2):
        while True:
            row: Final = await (
                await connection.execute(
                    """SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database()
                AND wait_event_type='Lock' AND query LIKE 'SELECT user_id FROM retail_wallets%%'"""
                )
            ).fetchone()
            if row is not None and row["n"] > 0:
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_request_waiting_before_policy_creation_uses_post_lock_admission_clock(runtime: BudgetRuntime) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await lock_budget_wallet(connection, "account")
        request: Final = asyncio.create_task(_reserve(runtime.peer, "waiting"))
        await _wallet_waiter(connection)
        await connection.execute(
            """INSERT INTO retail_model_budget_policies(id,user_id,model,ends_at,limit_nanos,created_by,updated_by)
            VALUES(%s,'account','public-model',clock_timestamp()+interval '1 hour',100000000,'admin','admin')""",
            (uuid4(),),
        )
    with pytest.raises(BudgetExceeded):
        await request
    assert (await runtime.store.get_wallet("account"))["reserved"] == "0.000000000"


@pytest.mark.asyncio
async def test_expiry_while_request_waits_for_wallet_is_checked_after_lock(runtime: BudgetRuntime) -> None:
    budget: Final = await _budget(runtime, limit=100000000, seconds=0.3)
    async with runtime.store.diagnostic_transaction() as connection:
        await lock_budget_wallet(connection, "account")
        request: Final = asyncio.create_task(_reserve(runtime.peer, "waiting-expiry"))
        await _wallet_waiter(connection)
        await connection.execute("SELECT pg_sleep(0.35)")
    assert (await request)["status"] == "reserved"
    assert (await _policy(runtime, budget.id)).held_nanos == 0
