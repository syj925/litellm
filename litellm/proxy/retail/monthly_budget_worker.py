import asyncio
from typing import Final

from litellm._logging import verbose_proxy_logger

from .control_plane_flags import monthly_budgets_enabled
from .monthly_budget_store import MonthlyBudgetStore
from .store import WalletStore


class MonthlyBudgetWorker:
    def __init__(self, store: MonthlyBudgetStore) -> None:
        self.store = store
        self._closing = False
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="retail-monthly-budget-worker")

    async def run_once(self) -> int:
        if not monthly_budgets_enabled():
            return 0
        return await self.store.materialize_batch()

    async def _run(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                if await self.run_once():
                    continue
            except Exception as exc:  # noqa: BLE001  # optional renewal outages must leave durable admission expectations intact
                verbose_proxy_logger.warning("Monthly budget renewal deferred (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=10)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)


class _Lifecycle:
    def __init__(self) -> None:
        self.current: MonthlyBudgetWorker | None = None
        self.lock = asyncio.Lock()


_lifecycle: Final = _Lifecycle()


async def start_monthly_budget_worker(wallet: WalletStore) -> None:
    async with _lifecycle.lock:
        if _lifecycle.current is not None or not monthly_budgets_enabled():
            return
        await wallet.initialize()
        selected: Final = MonthlyBudgetWorker(MonthlyBudgetStore(wallet))
        await selected.start()
        _lifecycle.current = selected


async def close_monthly_budget_worker() -> None:
    async with _lifecycle.lock:
        selected: Final = _lifecycle.current
        _lifecycle.current = None
    if selected is not None:
        await selected.close()
