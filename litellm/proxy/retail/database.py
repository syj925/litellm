import asyncio
import math
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Final

import psycopg
from anyio import fail_after
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout, TooManyRequests


def database_setting(name: str, default: float, minimum: float, maximum: float) -> float:
    value: Final = float(os.environ.get(name, str(default)))
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def database_integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    value: Final = database_setting(name, default, minimum, maximum)
    if not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    return int(value)


class _AccountGate:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class RetailDatabase:
    def __init__(
        self,
        database_url: str,
        *,
        finance_connections: int,
        diagnostic_connections: int,
        borrow_timeout: float,
        max_waiting: int,
        diagnostic_max_waiting: int,
        account_timeout: float,
        account_max_waiting: int,
        max_account_gates: int,
        critical_connections: int = 4,
        critical_max_waiting: int = 544,
        critical_borrow_timeout: float = 10,
        statement_timeout_ms: int = 5000,
        lock_timeout_ms: int = 1000,
        idle_transaction_timeout_ms: int = 10000,
    ) -> None:
        if (
            min(
                finance_connections,
                diagnostic_connections,
                critical_connections,
                critical_max_waiting,
                max_waiting,
                diagnostic_max_waiting,
                account_max_waiting,
                max_account_gates,
                statement_timeout_ms,
                lock_timeout_ms,
                idle_transaction_timeout_ms,
            )
            < 1
        ):
            raise ValueError("Database connection and waiting limits must be positive")
        if not all(
            math.isfinite(value) and value > 0 for value in (borrow_timeout, account_timeout, critical_borrow_timeout)
        ):
            raise ValueError("Database waiting timeouts must be finite and positive")
        self._timeout = borrow_timeout
        self._critical_timeout = critical_borrow_timeout
        existing_options: Final = conninfo_to_dict(database_url).get("options", "")
        self._options = (
            f"{existing_options} -c statement_timeout={statement_timeout_ms} -c lock_timeout={lock_timeout_ms} "
            f"-c idle_in_transaction_session_timeout={idle_transaction_timeout_ms}"
        ).strip()
        self._account_timeout = account_timeout
        self._account_max_waiting = account_max_waiting
        self._max_account_gates = max_account_gates
        self._accounts: dict[str, _AccountGate] = {}  # mutable-ok: bounded registry reclaims every idle entry
        self._opening = asyncio.Lock()
        self._opened = False
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        self._finance = self._pool(database_url, finance_connections, max_waiting, "retail-finance")
        self._diagnostic = self._pool(database_url, diagnostic_connections, diagnostic_max_waiting, "retail-diagnostic")
        self._critical = self._pool(
            database_url,
            critical_connections,
            critical_max_waiting,
            "retail-critical",
            critical_borrow_timeout,
            critical_connections,
        )

    def _pool(
        self, database_url: str, size: int, waiting: int, name: str, timeout: float | None = None, minimum: int = 0
    ) -> AsyncConnectionPool[psycopg.AsyncConnection]:
        selected_timeout: Final = self._timeout if timeout is None else timeout
        return AsyncConnectionPool(
            database_url,
            open=False,
            min_size=minimum,
            max_size=size,
            max_waiting=waiting,
            timeout=selected_timeout,
            name=name,
            check=AsyncConnectionPool.check_connection,
            kwargs={
                "row_factory": dict_row,
                "autocommit": True,
                "connect_timeout": min(10, max(1, math.ceil(selected_timeout))),
                "options": self._options,
            },
        )

    async def open(self) -> None:
        if self._opened and not self._closing:
            return
        async with self._opening:
            if self._closing:
                raise PoolClosed("Retail database pools are closed")
            if not self._opened:
                # Reserve physical terminal-write capacity before accepting preflight work.
                await self._critical.open(wait=True, timeout=self._critical_timeout)
                await self._finance.open()
                await self._diagnostic.open()
                self._opened = True

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close_pools())
        await asyncio.shield(self._close_task)

    async def _close_pools(self) -> None:
        async with self._opening:
            await asyncio.gather(self._finance.close(), self._diagnostic.close(), self._critical.close())

    @asynccontextmanager
    async def transaction(
        self, *, diagnostic: bool = False, critical: bool = False
    ) -> AsyncIterator[psycopg.AsyncConnection]:
        await self.open()
        if diagnostic and critical:
            raise ValueError("A transaction must select exactly one database budget")
        pool: Final = self._critical if critical else self._diagnostic if diagnostic else self._finance
        timeout: Final = self._critical_timeout if critical else self._timeout
        async with pool.connection(timeout=timeout) as connection:
            async with connection.transaction():
                yield connection

    @property
    def account_gate_count(self) -> int:
        return len(self._accounts)

    def pool_stats(self) -> Mapping[str, Mapping[str, int]]:
        return {
            "finance": self._finance.get_stats(),
            "diagnostic": self._diagnostic.get_stats(),
            "critical": self._critical.get_stats(),
        }

    @asynccontextmanager
    async def account(self, user_id: str) -> AsyncIterator[None]:
        existing: Final = self._accounts.get(user_id)
        if existing is None and len(self._accounts) >= self._max_account_gates:
            raise TooManyRequests("Retail account admission is full")
        gate: Final = existing if existing is not None else _AccountGate()
        if gate.users >= self._account_max_waiting + 1:
            raise TooManyRequests("Retail account waiting limit has been reached")
        self._accounts[user_id] = gate
        gate.users += 1
        try:
            try:
                with fail_after(self._account_timeout):
                    await gate.lock.acquire()
            except TimeoutError as exc:
                raise PoolTimeout("Retail account admission timed out") from exc
            try:
                yield None
            finally:
                gate.lock.release()
        finally:
            gate.users -= 1
            if gate.users == 0:
                self._accounts.pop(user_id, None)
