import asyncio
from typing import Final

import pytest
from psycopg_pool import PoolClosed, TooManyRequests

from litellm.proxy.retail.database import RetailDatabase, database_integer_setting, database_setting


def database() -> RetailDatabase:
    return RetailDatabase(
        "postgresql://not-used",
        finance_connections=2,
        diagnostic_connections=1,
        borrow_timeout=1,
        max_waiting=2,
        diagnostic_max_waiting=1,
        account_timeout=1,
        account_max_waiting=1,
        max_account_gates=1,
    )


@pytest.mark.parametrize("value", ("0", "-1", "nan", "inf", "200"))
def test_database_settings_reject_nonfinite_or_out_of_bounds_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("RETAIL_DB_LIMIT_TEST", value)
    with pytest.raises(ValueError):
        database_setting("RETAIL_DB_LIMIT_TEST", 16, 1, 128)


def test_integer_database_limits_are_not_silently_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_DB_LIMIT_TEST", "1.5")
    with pytest.raises(ValueError):
        database_integer_setting("RETAIL_DB_LIMIT_TEST", 16, 1, 128)


@pytest.mark.asyncio
async def test_lazy_construct_and_close_never_connect_and_closed_store_rejects_borrowing() -> None:
    store: Final = database()
    assert all(stats.get("connections_num", 0) == 0 for stats in store.pool_stats().values())
    await store.aclose()
    await store.aclose()
    with pytest.raises(PoolClosed):
        async with store.transaction():
            pytest.fail("Closed pool handed out a connection")


@pytest.mark.asyncio
async def test_cancelled_account_waiter_at_lock_handoff_does_not_poison_admission() -> None:
    store: Final = database()

    async def waiter() -> None:
        async with store.account("hot"):
            await asyncio.Event().wait()

    for _ in range(4):
        held: Final = store.account("hot")
        await held.__aenter__()
        waiting: Final = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        await held.__aexit__(None, None, None)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert store.account_gate_count == 0
        async with store.account("hot"):
            assert store.account_gate_count == 1
    assert store.account_gate_count == 0
    await store.aclose()


@pytest.mark.asyncio
async def test_bounded_account_admission_cancels_waiters_and_reclaims_entries() -> None:
    store: Final = database()
    entered: Final = asyncio.Event()
    release: Final = asyncio.Event()

    async def holder() -> None:
        async with store.account("hot"):
            entered.set()
            await release.wait()

    async def waiter() -> None:
        async with store.account("hot"):
            pytest.fail("Waiter entered while holder still owned the account")

    task: Final = asyncio.create_task(holder())
    await entered.wait()
    waiting: Final = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    with pytest.raises(TooManyRequests):
        async with store.account("hot"):
            pytest.fail("Too many callers admitted to one account")
    with pytest.raises(TooManyRequests):
        async with store.account("other"):
            pytest.fail("Registry exceeded its bound")
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert store.account_gate_count == 1
    release.set()
    await task
    assert store.account_gate_count == 0
    async with store.account("other"):
        assert store.account_gate_count == 1
    assert store.account_gate_count == 0
    await store.aclose()
