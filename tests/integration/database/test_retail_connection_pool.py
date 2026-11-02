import asyncio
import os
from collections.abc import Mapping
from typing import Final
from uuid import uuid4

import psycopg
import pytest
from anyio import CancelScope
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import PoolTimeout, TooManyRequests
from pydantic import JsonValue

from litellm.proxy.retail.database import RetailDatabase
from litellm.proxy.retail.store import WalletStore


@pytest.mark.asyncio
async def test_retail_connection_reuse_transactions_recovery_isolation_and_fairness() -> None:
    configured: Final = os.environ.get("RETAIL_TEST_DATABASE_URL")
    if not configured:
        pytest.skip("An isolated PostgreSQL service with CREATE DATABASE permission is required")
    base: Final = conninfo_to_dict(configured)
    name: Final = "retail_pool_" + uuid4().hex
    dsn: Final = make_conninfo(**{**base, "dbname": name})
    with psycopg.connect(**base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    store: Final = WalletStore(
        dsn,
        max_connections=2,
        diagnostic_connections=1,
        borrow_timeout=0.5,
        max_waiting=1,
        critical_connections=1,
        critical_max_waiting=1,
        critical_borrow_timeout=0.5,
    )
    try:
        async with await psycopg.AsyncConnection.connect(dsn) as setup:
            await setup.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        await store.initialize()
        await _critical_startup_reserves_physical_connections(store, dsn)
        await _critical_startup_fails_if_server_capacity_is_insufficient(dsn)
        await _reuse_and_recover(store, dsn)
        await _rollback_and_cancel(store)
        await _bounded_borrow_and_isolation(store)
        await _critical_budget_isolation_and_bounds(store)
        await _account_fairness(store, dsn)
        await _external_lock_timeout_rolls_back(store, dsn)
        await _statement_and_idle_timeouts_recover(store, dsn)
        await _billing_context(store)
    finally:
        await store.aclose()
        with psycopg.connect(**base, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def _critical_startup_reserves_physical_connections(store: WalletStore, dsn: str) -> None:
    assert store.pool_stats()["critical"]["pool_available"] == 1
    database: Final = RetailDatabase(
        dsn,
        finance_connections=2,
        diagnostic_connections=1,
        borrow_timeout=0.5,
        max_waiting=1,
        diagnostic_max_waiting=1,
        account_timeout=1,
        account_max_waiting=1,
        max_account_gates=1,
        critical_connections=2,
        critical_max_waiting=2,
        critical_borrow_timeout=1,
    )
    try:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as probe:
            before: Final = frozenset(
                row[0]
                for row in await (
                    await probe.execute("SELECT pid FROM pg_stat_activity WHERE datname=current_database()")
                ).fetchall()
            )
            await database.open()
            after: Final = frozenset(
                row[0]
                for row in await (
                    await probe.execute("SELECT pid FROM pg_stat_activity WHERE datname=current_database()")
                ).fetchall()
            )
            assert len(after - before) == 2
            assert database.pool_stats()["critical"]["pool_available"] == 2
            assert database.pool_stats()["finance"].get("connections_num", 0) == 0
            assert database.pool_stats()["diagnostic"].get("connections_num", 0) == 0
    finally:
        await database.aclose()


async def _critical_startup_fails_if_server_capacity_is_insufficient(dsn: str) -> None:
    role: Final = "retail_pool_capacity_" + uuid4().hex
    fixture_password: Final = uuid4().hex
    limited_dsn: Final = make_conninfo(dsn, user=role, password=fixture_password)
    limited: Final = WalletStore(limited_dsn, critical_connections=2, critical_borrow_timeout=0.2)
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} CONNECTION LIMIT 1").format(
                sql.Identifier(role),
                sql.Literal(fixture_password),
            )
        )
        try:
            with pytest.raises(PoolTimeout):
                await limited.initialize()
            assert limited.pool_stats()["finance"].get("connections_num", 0) == 0
            assert limited.pool_stats()["diagnostic"].get("connections_num", 0) == 0
        finally:
            await limited.aclose()
            row: Final = await (
                await admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE usename=%s",
                    (role,),
                )
            ).fetchone()
            await admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
            assert row is not None and row[0] == 0


async def _pid(store: WalletStore) -> int:
    async with store.transaction() as connection:
        row: Final = await (await connection.execute("SELECT pg_backend_pid() AS pid")).fetchone()
        assert isinstance(row, dict)
        return int(row["pid"])


async def _reuse_and_recover(store: WalletStore, dsn: str) -> None:
    first: Final = await _pid(store)
    assert all(pid == first for pid in (await _pid(store), await _pid(store), await _pid(store)))
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute("SELECT pg_terminate_backend(%s)", (first,))
    assert await _pid(store) != first
    assert store.pool_stats()["finance"]["pool_size"] <= 2


async def _count(store: WalletStore) -> int:
    async with store.transaction() as connection:
        row: Final = await (await connection.execute("SELECT count(*) AS count FROM pool_probe")).fetchone()
        assert isinstance(row, dict)
        return int(row["count"])


async def _rollback_and_cancel(store: WalletStore) -> None:
    async with store.transaction() as connection:
        await connection.execute("CREATE TABLE pool_probe(value INT)")
    with pytest.raises(ValueError):
        async with store.transaction() as connection:
            await connection.execute("INSERT INTO pool_probe VALUES (1)")
            raise ValueError("abort transaction")
    assert await _count(store) == 0
    for _ in range(2):
        with CancelScope() as cancellation:
            async with store.transaction() as connection:
                await connection.execute("INSERT INTO pool_probe VALUES (3)")
                cancellation.cancel()
                await asyncio.sleep(0)
    assert await _count(store) == 0
    written: Final = asyncio.Event()
    never: Final = asyncio.Event()

    async def cancelled_write() -> None:
        async with store.transaction() as connection:
            await connection.execute("INSERT INTO pool_probe VALUES (2)")
            written.set()
            await never.wait()

    task: Final = asyncio.create_task(cancelled_write())
    await asyncio.wait_for(written.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _count(store) == 0


async def _bounded_borrow_and_isolation(store: WalletStore) -> None:
    release: Final = asyncio.Event()
    all_borrowed: Final = asyncio.Barrier(3)

    async def holder() -> None:
        async with store.transaction():
            await all_borrowed.wait()
            await release.wait()

    holders: Final = (asyncio.create_task(holder()), asyncio.create_task(holder()))
    await asyncio.wait_for(all_borrowed.wait(), timeout=5)

    async def borrow() -> None:
        async with store.transaction():
            pytest.fail("Pool loaned more than its configured maximum")

    try:
        waiting: Final = asyncio.create_task(borrow())

        async def queued() -> None:
            while store.pool_stats()["finance"].get("requests_waiting", 0) != 1:
                await asyncio.sleep(0)

        await asyncio.wait_for(queued(), timeout=5)
        with pytest.raises(TooManyRequests):
            await borrow()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        async with store.diagnostic_transaction() as connection:
            row: Final = await (await connection.execute("SELECT 42 AS value")).fetchone()
            assert isinstance(row, dict) and row["value"] == 42
    finally:
        release.set()
        await asyncio.gather(*holders)
    async with store.transaction():
        async with store.transaction():
            with pytest.raises(PoolTimeout):
                await borrow()
    async with store.diagnostic_transaction():
        assert await _pid(store)
    assert store.pool_stats()["finance"].get("requests_waiting", 0) == 0


async def _critical_budget_isolation_and_bounds(store: WalletStore) -> None:
    # Saturated preflight and diagnostics cannot take the terminal-fact budget.
    async with store.transaction(), store.transaction(), store.diagnostic_transaction():
        with pytest.raises(ValueError):
            async with store.critical_transaction() as connection:
                await connection.execute("INSERT INTO pool_probe VALUES (6)")
                raise ValueError("rollback critical transaction")
        async with store.critical_transaction() as connection:
            row: Final = await (await connection.execute("SELECT count(*) AS count FROM pool_probe")).fetchone()
            assert isinstance(row, dict) and row["count"] == 0

            async def borrow() -> None:
                async with store.critical_transaction():
                    pytest.fail("Critical budget exceeded its connection limit")

            waiting: Final = asyncio.create_task(borrow())

            async def queued() -> None:
                while store.pool_stats()["critical"].get("requests_waiting", 0) != 1:
                    await asyncio.sleep(0)

            await asyncio.wait_for(queued(), timeout=5)
            with pytest.raises(TooManyRequests):
                await borrow()
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
    async with store.critical_transaction():
        with pytest.raises(PoolTimeout):
            await borrow()
    assert store.pool_stats()["critical"].get("requests_waiting", 0) == 0
    assert await _count(store) == 0


async def _account_fairness(store: WalletStore, dsn: str) -> None:
    for user in ("hot", "other"):
        await store.get_wallet(user, create=True)
        await store.credit(user, "10", user, "fixture", "admin", user)
    requests: Final = tuple("hot-" + str(index) for index in range(3))

    async def hot_request(request: str) -> Mapping[str, JsonValue]:
        await store.get_billing_context("hot", "model")
        return await store.reserve("hot", request, "model", "0.01", {})

    async with await psycopg.AsyncConnection.connect(dsn) as blocker:
        await blocker.execute("SELECT user_id FROM retail_wallets WHERE user_id='hot' FOR UPDATE")
        tasks: Final = tuple(asyncio.create_task(hot_request(request)) for request in requests)

        async def blocked() -> None:
            while True:
                async with store.diagnostic_transaction() as probe:
                    row: Final = await (
                        await probe.execute(
                            "SELECT count(*) AS count FROM pg_stat_activity WHERE datname=current_database() "
                            "AND wait_event_type='Lock' AND query LIKE 'SELECT user_id FROM retail_wallets%'"
                        )
                    ).fetchone()
                if isinstance(row, dict) and row["count"]:
                    return
                await asyncio.sleep(0)

        await asyncio.wait_for(blocked(), timeout=5)
        await store.get_billing_context("other", "model")
        unrelated: Final = await store.reserve("other", "other-request", "model", "0.01", {})
        assert unrelated["status"] == "reserved"
        await blocker.rollback()
        results: Final = await asyncio.gather(*tasks)
    assert all(result["status"] == "reserved" for result in results)
    assert store.account_gate_count == 0
    for request in (*requests, "other-request"):
        await store.release(request, "fixture cleanup")
    assert (await store.get_wallet("hot"))["pending_requests"] == 0


async def _billing_context(store: WalletStore) -> None:
    async with store.transaction() as connection:
        before: Final = await (
            await connection.execute(
                "SELECT row_to_json(w) AS wallet_record,w.xmin::text AS version FROM retail_wallets w WHERE user_id='hot'"
            )
        ).fetchone()
    context: Final = await store.get_billing_context("hot", "model")
    assert set(context) == {"price", "discount"}
    assert context["discount"] == await store.get_model_discount("hot", "model")
    assert context["price"] == await store.get_model_price("model")
    async with store.transaction() as connection:
        after: Final = await (
            await connection.execute(
                "SELECT row_to_json(w) AS wallet_record,w.xmin::text AS version FROM retail_wallets w WHERE user_id='hot'"
            )
        ).fetchone()
    assert after == before
    created: Final = await store.get_billing_context("new-context-wallet", "model")
    wallet: Final = await store.get_wallet("new-context-wallet")
    assert wallet["balance"] == wallet["reserved"] == "0.000000000"
    assert created["discount"] == await store.get_model_discount("new-context-wallet", "model")
    assert created["price"] == context["price"]


async def _external_lock_timeout_rolls_back(store: WalletStore, dsn: str) -> None:
    before: Final = await store.get_wallet("other")
    async with await psycopg.AsyncConnection.connect(dsn) as blocker:
        await blocker.execute("SELECT user_id FROM retail_wallets WHERE user_id='other' FOR UPDATE")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await store.reserve("other", "lock-timeout-request", "model", "0.01", {})
    assert await store.get_reservation("lock-timeout-request") is None
    assert await store.get_wallet("other") == before
    assert store.account_gate_count == 0
    assert await _pid(store)
    for transaction in (store.transaction, store.diagnostic_transaction, store.critical_transaction):
        manager: Final = transaction()
        async with manager as connection:
            row: Final = await (
                await connection.execute(
                    "SELECT current_setting('statement_timeout') AS statement, current_setting('lock_timeout') AS lock, "
                    "current_setting('idle_in_transaction_session_timeout') AS idle"
                )
            ).fetchone()
            assert isinstance(row, dict)
            assert row == {"statement": "5s", "lock": "1s", "idle": "10s"}


async def _statement_and_idle_timeouts_recover(store: WalletStore, dsn: str) -> None:
    bounded: Final = WalletStore(dsn, max_connections=1, statement_timeout_ms=100, idle_transaction_timeout_ms=100)
    try:
        with pytest.raises(psycopg.errors.QueryCanceled):
            async with bounded.transaction() as connection:
                await connection.execute("INSERT INTO pool_probe VALUES (4)")
                await connection.execute("SELECT pg_sleep(0.25)")
        assert await _count(store) == 0
        with pytest.raises(psycopg.errors.IdleInTransactionSessionTimeout):
            async with bounded.transaction() as connection:
                await connection.execute("INSERT INTO pool_probe VALUES (5)")
                await asyncio.sleep(0.2)
                await connection.execute("SELECT 1")
        assert await _count(store) == 0
        assert await _pid(bounded)
    finally:
        await bounded.aclose()
