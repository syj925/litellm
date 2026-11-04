import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from typing import Final
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import JsonValue

from litellm import Router
from litellm.proxy import proxy_server
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail.billing import RetailBilling, reservation_id
from litellm.proxy.retail.models import IdempotencyConflict, InsufficientFunds, ParallelLimitExceeded
from litellm.proxy.retail.settlement_store import claim_facts, record_facts, release_claims
from litellm.proxy.retail.settlement_worker import SettlementWorker, finish_request_activity, register_request_activity
from litellm.proxy.retail.store import WalletStore


@pytest_asyncio.fixture(loop_scope="function")
async def settlement_store() -> AsyncIterator[WalletStore]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated settlement-test PostgreSQL service is required")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_settlement_" + uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    store: Final = WalletStore(make_conninfo(**{**base, "dbname": name}))
    try:
        async with await psycopg.AsyncConnection.connect(store.database_url) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        await store.initialize()
        await store.get_wallet("user", create=True)
        await store.credit("user", "1", "fixture", "fixture", "fixture", "fixture")
        yield store
    finally:
        await store.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def _reserve(store: WalletStore, request_id: str) -> None:
    await store.reserve(
        "user",
        request_id,
        "model",
        "0.01",
        {
            "version": "token-v3",
            "input_cost_per_token": "0.000002",
            "output_cost_per_token": "0.000006",
            "cache_read_input_token_cost": "0",
            "cache_creation_input_token_cost": "0",
            "multiplier": "1",
        },
        max_parallel=512,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "supported"),
    (
        (None, False),
        ({"expression": "p * 0.1 + c * 0.1"}, False),
        (
            {
                "expression": "p * 0.1 + c * 0.1 + fixed(u('web_search_requests') * 0.01)",
                "usage_bounds": {"web_search_requests": "1"},
            },
            True,
        ),
    ),
)
async def test_server_priced_tools_require_an_explicit_supported_price_card(
    settlement_store: WalletStore,
    monkeypatch: pytest.MonkeyPatch,
    policy: Mapping[str, JsonValue] | None,
    supported: bool,
) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    router: Final = Router(
        model_list=[
            {
                "model_name": "search-model",
                "litellm_params": {"model": "openai/fixture-model", "api_key": "fixture"},
            }
        ]
    )
    monkeypatch.setattr(proxy_server, "llm_router", router)
    await settlement_store.save_model_price(
        "search-model",
        {
            "input_cost_per_token": "0.0000001",
            "output_cost_per_token": "0.0000001",
            "cache_read_input_token_cost": "0",
            "cache_creation_input_token_cost": "0",
            "expression_policy": dict(policy) if policy is not None else None,
        },
    )
    callback: Final = RetailBilling(settlement_store)
    user: Final = UserAPIKeyAuth(user_id="user", user_role=LitellmUserRoles.INTERNAL_USER)
    request: Final = {"model": "search-model", "tools": [{"type": "web_search"}]}
    try:
        if not supported:
            with pytest.raises(HTTPException) as failure:
                await callback.async_pre_call_hook(user, None, request, "aresponses")
            assert failure.value.status_code == 400
            assert failure.value.detail == (
                "Server-priced search requires an expression with reported web_search_requests"
            )
            assert (await settlement_store.get_wallet("user"))["reserved"] == "0.000000000"
            return
        prepared: Final = await callback.async_pre_call_hook(user, None, request, "aresponses")
        identifier: Final = reservation_id(prepared)
        assert identifier is not None
        reservation: Final = await settlement_store.get_reservation(identifier)
        assert reservation is not None and reservation["status"] == "reserved"
        assert reservation["price_snapshot"]["version"] == "expression-v1"
        await finish_request_activity(identifier, settlement_store)
        await settlement_store.release(identifier, "Fixture completed without forwarding")
    finally:
        router.cache.flush_cache()


@pytest.mark.asyncio
async def test_persisted_usage_recovers_after_lease_expiry_and_duplicate_callbacks_charge_once(
    settlement_store: WalletStore,
) -> None:
    store: Final = settlement_store
    await _reserve(store, "recover")
    facts: Final = {"usage": {"input_tokens": 10, "output_tokens": 2}}
    await record_facts(store, "recover", "0.000032", facts)
    abandoned: Final = await claim_facts(store, "dead-worker", 1, 60)
    assert len(abandoned) == 1
    async with store.transaction() as connection:
        await connection.execute("UPDATE retail_settlement_facts SET lease_until=now()-interval '1 second'")
    await asyncio.gather(*(record_facts(store, "recover", "0.000032", facts) for _ in range(8)))
    await asyncio.gather(SettlementWorker(store).run_once(), SettlementWorker(store).run_once())
    await SettlementWorker(store).run_once()
    reservation: Final = await store.get_reservation("recover")
    assert reservation is not None and reservation["status"] == "settled"
    assert reservation["amount"] == "0.000032000"
    async with store.transaction() as connection:
        row: Final = await (
            await connection.execute(
                "SELECT count(*) AS total,sum(amount_nanos) AS total_amount FROM retail_ledger WHERE kind='charge'",
            )
        ).fetchone()
    assert row is not None and row["total"] == 1 and row["total_amount"] == -32000
    wallet: Final = await store.get_wallet("user")
    assert wallet["balance"] == "0.999968000" and wallet["reserved"] == "0.000000000"


@pytest.mark.asyncio
async def test_unknown_usage_keeps_money_held_but_releases_the_active_request_lease(
    settlement_store: WalletStore,
) -> None:
    store: Final = settlement_store
    await _reserve(store, "unknown")
    register_request_activity("unknown", store)
    await record_facts(store, "unknown", None, {"billing_error": "reported_usage_missing"})
    await finish_request_activity("unknown", store)
    assert await SettlementWorker(store).run_once() == 0
    reservation: Final = await store.get_reservation("unknown")
    wallet: Final = await store.get_wallet("user")
    assert reservation is not None and reservation["status"] == "reserved"
    assert wallet["reserved"] == "0.010000000" and wallet["pending_requests"] == 0
    await record_facts(store, "unknown", "0.000032", {"usage": {"input_tokens": 10, "output_tokens": 2}})
    assert await SettlementWorker(store).run_once() == 1
    assert (await store.get_wallet("user"))["reserved"] == "0.000000000"


@pytest.mark.asyncio
async def test_manual_resolution_and_a_worker_race_never_debit_twice(settlement_store: WalletStore) -> None:
    store: Final = settlement_store
    await _reserve(store, "manual")
    await record_facts(store, "manual", "0.000032", {"usage": {"input_tokens": 10, "output_tokens": 2}})
    await store.release(
        "manual",
        "Verified manual resolution",
        {
            "actor_id": "fixture-admin",
            "reference": "resolution",
            "reason": "manual",
            "idempotency_key": "resolve",
        },
    )
    assert await SettlementWorker(store).run_once() == 1
    wallet: Final = await store.get_wallet("user")
    assert wallet["balance"] == "1.000000000" and wallet["total_spent"] == "0.000000000"
    with pytest.raises(IdempotencyConflict):
        await record_facts(store, "manual", "0.000033", {"usage": {"input_tokens": 10, "output_tokens": 2}})


@pytest.mark.asyncio
async def test_claim_is_exclusive_and_graceful_release_is_immediately_reclaimable(
    settlement_store: WalletStore,
) -> None:
    store: Final = settlement_store
    for index in range(8):
        await _reserve(store, "claim-" + str(index))
        await record_facts(
            store, "claim-" + str(index), "0.000032", {"usage": {"input_tokens": 10, "output_tokens": 2}}
        )
    claims: Final = await asyncio.gather(claim_facts(store, "one", 8, 60), claim_facts(store, "two", 8, 60))
    assert len({fact.request_id for batch in claims for fact in batch}) == 8
    assert sum(len(batch) for batch in claims) == 8
    await release_claims(store, "one")
    await release_claims(store, "two")
    assert len(await claim_facts(store, "new-worker", 8, 60)) == 8


@pytest.mark.asyncio
async def test_stream_terminal_is_not_yielded_until_its_facts_are_committed(settlement_store: WalletStore) -> None:
    store: Final = settlement_store
    await _reserve(store, "terminal")
    callback: Final = RetailBilling(store)

    async def stream() -> AsyncIterator[object]:
        yield {"choices": [{"delta": {"content": "private response"}}]}
        yield {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
        yield "data: [DONE]\n\n"

    wrapped: Final = callback.async_post_call_streaming_iterator_hook(
        UserAPIKeyAuth(user_id="user"),
        stream(),
        {"metadata": {"retail_request_id": "terminal"}, "stream": True},
    )
    await anext(wrapped)
    async with store.transaction() as connection:
        before: Final = await (
            await connection.execute("SELECT count(*) AS total FROM retail_settlement_facts")
        ).fetchone()
    assert before is not None and before["total"] == 0
    assert (await anext(wrapped))["usage"]["completion_tokens"] == 2
    async with store.transaction() as connection:
        committed: Final = await (
            await connection.execute(
                "SELECT state,amount_nanos,details FROM retail_settlement_facts WHERE request_id='terminal'",
            )
        ).fetchone()
    assert committed is not None and committed["state"] == "pending" and committed["amount_nanos"] == 32000
    assert "private response" not in str(committed)
    assert await anext(wrapped) == "data: [DONE]\n\n"
    with pytest.raises(StopAsyncIteration):
        await anext(wrapped)
    await wrapped.aclose()


@pytest.mark.asyncio
async def test_a_failed_fact_commit_never_yields_the_terminal_usage_or_done(settlement_store: WalletStore) -> None:
    store: Final = settlement_store
    await _reserve(store, "blocked")
    async with store.transaction() as connection:
        await connection.execute("""CREATE FUNCTION reject_billing_facts() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'fixture fact storage unavailable'; END $$""")
        await connection.execute("""CREATE TRIGGER reject_billing_facts BEFORE INSERT ON retail_settlement_facts
          FOR EACH ROW EXECUTE FUNCTION reject_billing_facts()""")

    async def stream() -> AsyncIterator[object]:
        yield {"choices": [{"delta": {"content": "text already delivered"}}]}
        yield {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
        yield "data: [DONE]\n\n"

    wrapped: Final = RetailBilling(store).async_post_call_streaming_iterator_hook(
        UserAPIKeyAuth(user_id="user"),
        stream(),
        {"metadata": {"retail_request_id": "blocked"}, "stream": True},
    )
    assert (await anext(wrapped))["choices"][0]["delta"]["content"] == "text already delivered"
    with pytest.raises(psycopg.errors.RaiseException, match="fixture fact storage unavailable"):
        await anext(wrapped)
    reservation: Final = await store.get_reservation("blocked")
    assert reservation is not None and reservation["status"] == "reserved"
    async with store.transaction() as connection:
        row: Final = await (
            await connection.execute("SELECT count(*) AS total FROM retail_settlement_facts")
        ).fetchone()
    assert row is not None and row["total"] == 0
    await wrapped.aclose()


@pytest.mark.asyncio
async def test_a_rolled_back_charge_is_retried_from_the_same_durable_usage(settlement_store: WalletStore) -> None:
    store: Final = settlement_store
    await _reserve(store, "retry")
    await record_facts(store, "retry", "0.000032", {"usage": {"input_tokens": 10, "output_tokens": 2}})
    async with store.transaction() as connection:
        await connection.execute("""CREATE FUNCTION reject_charge() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'fixture transient write failure'; END $$""")
        await connection.execute("""CREATE TRIGGER reject_charge BEFORE INSERT ON retail_ledger
          FOR EACH ROW EXECUTE FUNCTION reject_charge()""")
    assert await SettlementWorker(store).run_once() == 1
    assert (await store.get_wallet("user"))["balance"] == "1.000000000"
    async with store.transaction() as connection:
        row: Final = await (
            await connection.execute(
                "SELECT state,attempts,last_error_type FROM retail_settlement_facts WHERE request_id='retry'",
            )
        ).fetchone()
        await connection.execute("DROP TRIGGER reject_charge ON retail_ledger")
        await connection.execute("DROP FUNCTION reject_charge()")
        await connection.execute("UPDATE retail_settlement_facts SET next_attempt_at=now() WHERE request_id='retry'")
    assert row is not None and row["state"] == "pending" and row["attempts"] == 1
    assert row["last_error_type"] == "RaiseException"
    assert await SettlementWorker(store).run_once() == 1
    assert (await store.get_wallet("user"))["balance"] == "0.999968000"


@pytest.mark.asyncio
async def test_concurrent_manual_release_and_worker_settlement_preserve_a_single_financial_outcome(
    settlement_store: WalletStore,
) -> None:
    store: Final = settlement_store
    await _reserve(store, "race")
    await record_facts(store, "race", "0.000032", {"usage": {"input_tokens": 10, "output_tokens": 2}})
    results: Final = await asyncio.gather(
        SettlementWorker(store).run_once(),
        store.release(
            "race",
            "manual",
            {
                "actor_id": "fixture-admin",
                "reference": "race",
                "reason": "manual",
                "idempotency_key": "race",
            },
        ),
        return_exceptions=True,
    )
    assert all(not isinstance(result, Exception) or isinstance(result, IdempotencyConflict) for result in results)
    reservation: Final = await store.get_reservation("race")
    assert reservation is not None and reservation["status"] in {"settled", "released"}
    wallet: Final = await store.get_wallet("user")
    assert wallet["reserved"] == "0.000000000"
    assert wallet["balance"] == ("0.999968000" if reservation["status"] == "settled" else "1.000000000")


@pytest.mark.asyncio
async def test_a_completion_marker_failure_waits_for_its_claimed_siblings_before_accepting_more_work(
    settlement_store: WalletStore,
) -> None:
    store: Final = settlement_store
    for request_id in ("fast-finish-error", "slow-charge"):
        await _reserve(store, request_id)
        await record_facts(store, request_id, "0.000032", {"usage": {"input_tokens": 10, "output_tokens": 2}})
    async with store.transaction() as connection:
        await connection.execute("""CREATE FUNCTION fail_finish_marker() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF NEW.request_id='fast-finish-error' AND NEW.state='done' THEN
            RAISE EXCEPTION 'fixture completion marker unavailable'; END IF; RETURN NEW; END $$""")
        await connection.execute("""CREATE TRIGGER fail_finish_marker BEFORE UPDATE ON retail_settlement_facts
          FOR EACH ROW EXECUTE FUNCTION fail_finish_marker()""")
        await connection.execute("""CREATE FUNCTION slow_charge() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF NEW.request_id='slow-charge' THEN PERFORM pg_sleep(0.3); END IF; RETURN NEW; END $$""")
        await connection.execute("""CREATE TRIGGER slow_charge BEFORE INSERT ON retail_ledger
          FOR EACH ROW EXECUTE FUNCTION slow_charge()""")
    with pytest.raises(psycopg.errors.RaiseException, match="fixture completion marker unavailable"):
        await SettlementWorker(store, concurrency=2).run_once()
    slow: Final = await store.get_reservation("slow-charge")
    assert slow is not None and slow["status"] == "settled"


@pytest.mark.asyncio
async def test_an_inflight_terminal_needs_no_finance_connection_even_without_the_preflight_context(
    settlement_store: WalletStore,
) -> None:
    store: Final = settlement_store
    await _reserve(store, "isolated-terminal")

    async def stream() -> AsyncIterator[object]:
        yield {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
        yield "data: [DONE]\n\n"

    wrapped: Final = RetailBilling(store).async_post_call_streaming_iterator_hook(
        UserAPIKeyAuth(user_id="user"),
        stream(),
        {"metadata": {"retail_request_id": "isolated-terminal"}},
    )
    async with AsyncExitStack() as occupied:
        for _ in range(16):
            connection: Final = await occupied.enter_async_context(store.transaction())
            await connection.execute("SELECT 1")
        assert store.pool_stats()["finance"]["pool_available"] == 0
        usage_chunk: Final = await asyncio.wait_for(anext(wrapped), timeout=2)
        assert usage_chunk["usage"]["completion_tokens"] == 2
        assert await anext(wrapped) == "data: [DONE]\n\n"
    await wrapped.aclose()
    assert await SettlementWorker(store).run_once() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "max_parallel", "error", "held"),
    [("0.75", 512, InsufficientFunds, "0.750000000"), ("0.1", 1, ParallelLimitExceeded, "0.100000000")],
)
async def test_concurrent_reservations_recheck_balance_and_activity_after_locking_the_wallet(
    settlement_store: WalletStore, amount: str, max_parallel: int, error: type[Exception], held: str,
) -> None:
    store: Final = settlement_store
    peer: Final = WalletStore(store.database_url)
    try:
        async with await psycopg.AsyncConnection.connect(store.database_url) as blocker:
            await blocker.execute("SELECT user_id FROM retail_wallets WHERE user_id='user' FOR UPDATE")
            pending: Final = asyncio.gather(
                store.reserve("user", "concurrent-one", "model", amount, {}, max_parallel),
                peer.reserve("user", "concurrent-two", "model", amount, {}, max_parallel),
                return_exceptions=True,
            )
            try:
                async with await psycopg.AsyncConnection.connect(store.database_url, autocommit=True) as observer:
                    async def both_wait_for_the_wallet() -> None:
                        while True:
                            row: Final = await (
                                await observer.execute(
                                    """SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
                                       AND wait_event_type='Lock' AND query LIKE
                                       'SELECT user_id FROM retail_wallets WHERE user_id=% FOR UPDATE'"""
                                )
                            ).fetchone()
                            if row is not None and row[0] == 2:
                                return
                            await asyncio.sleep(0.005)

                    await asyncio.wait_for(both_wait_for_the_wallet(), timeout=0.75)
            finally:
                await blocker.rollback()
                results: Final = await pending
    finally:
        await peer.aclose()
    assert sum(isinstance(result, error) for result in results) == 1
    assert sum(isinstance(result, dict) for result in results) == 1
    wallet: Final = await store.get_wallet("user")
    assert wallet["balance"] == "1.000000000"
    assert wallet["reserved"] == held
    assert wallet["pending_requests"] == 1


@pytest.mark.asyncio
async def test_duplicate_reserve_updates_the_hold_once_and_rejects_changed_frozen_data(
    settlement_store: WalletStore,
) -> None:
    store: Final = settlement_store
    await asyncio.gather(*(_reserve(store, "same-request") for _ in range(8)))
    wallet: Final = await store.get_wallet("user")
    assert wallet["reserved"] == "0.010000000" and wallet["pending_requests"] == 1
    with pytest.raises(IdempotencyConflict):
        await store.reserve("user", "same-request", "model", "0.02", {})
    assert await store.get_wallet("user") == wallet


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["settle", "release", "record_pending_usage"])
async def test_reservation_mutations_lock_the_wallet_before_waiting_for_the_reservation(
    settlement_store: WalletStore, operation: str,
) -> None:
    store: Final = settlement_store
    await _reserve(store, "lock-order")
    async with await psycopg.AsyncConnection.connect(store.database_url) as blocker:
        await blocker.execute("SELECT request_id FROM retail_reservations WHERE request_id='lock-order' FOR UPDATE")
        pending: Final = asyncio.create_task(
            store.settle("lock-order", "0.000032", {}) if operation == "settle"
            else store.release("lock-order", "Rejected") if operation == "release"
            else store.record_pending_usage("lock-order", {"billing_error": "missing_usage"})
        )
        try:
            async with await psycopg.AsyncConnection.connect(store.database_url, autocommit=True) as observer:
                async def awaiting_reservation_lock() -> None:
                    while True:
                        row: Final = await (
                            await observer.execute(
                                """SELECT 1 FROM pg_stat_activity WHERE datname=current_database()
                                   AND wait_event_type='Lock' AND query LIKE
                                   'SELECT * FROM retail_reservations WHERE request_id=% FOR UPDATE'"""
                            )
                        ).fetchone()
                        if row is not None:
                            return
                        await asyncio.sleep(0.005)

                await asyncio.wait_for(awaiting_reservation_lock(), timeout=0.75)
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await observer.execute("SELECT user_id FROM retail_wallets WHERE user_id='user' FOR UPDATE NOWAIT")
        finally:
            await blocker.rollback()
            await pending
    reservation: Final = await store.get_reservation("lock-order")
    assert reservation is not None
    assert reservation["status"] == {
        "settle": "settled", "release": "released", "record_pending_usage": "reserved",
    }[operation]


@pytest.mark.asyncio
async def test_atomic_wallet_updates_supply_the_exact_ledger_balance_for_charge_release_and_late_usage(
    settlement_store: WalletStore,
) -> None:
    store: Final = settlement_store
    for request_id in ("charge-one", "release-one", "late-charge"):
        await _reserve(store, request_id)
    await store.settle("charge-one", "0.1", {})
    await store.release("release-one", "Rejected without usage")
    await store.release("late-charge", "Initially rejected without usage")
    await store.settle("late-charge", "0.2", {})
    await store.settle("charge-one", "0.1", {})
    await store.release("release-one", "Rejected without usage")
    wallet: Final = await store.get_wallet("user")
    assert wallet["balance"] == "0.700000000"
    assert wallet["reserved"] == "0.000000000"
    assert wallet["total_spent"] == "0.300000000"
    async with store.transaction() as connection:
        rows: Final = await (
            await connection.execute(
                "SELECT request_id,kind,amount_nanos,balance_after_nanos FROM retail_ledger WHERE request_id IS NOT NULL",
            )
        ).fetchall()
    ledger: Final = {
        (row["request_id"], row["kind"]): (row["amount_nanos"], row["balance_after_nanos"]) for row in rows
    }
    assert len(rows) == 4
    assert ledger[("charge-one", "charge")] == (-100000000, 900000000)
    assert ledger[("release-one", "release")] == (0, 900000000)
    assert ledger[("late-charge", "release")] == (0, 900000000)
    assert ledger[("late-charge", "charge")] == (-200000000, 700000000)
