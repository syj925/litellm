import asyncio
from typing import Final
from uuid import uuid4

from litellm._logging import verbose_proxy_logger

from .control_plane_flags import report_export_enabled
from .report_store import ReportError, ReportStore, get_report_store
from .store import WalletStore


class ReportWorker:
    def __init__(self, store: ReportStore) -> None:
        self.store = store
        self.owner = str(uuid4())
        self._closing = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.store.initialize()
        self._task = asyncio.create_task(self._run(), name="retail-report-worker")

    async def run_once(self) -> int:
        if not report_export_enabled():
            return 0
        await self.store.expire()
        claim: Final = await self.store.claim(self.owner)
        if claim is None:
            return 0
        try:
            async with asyncio.timeout(20):
                await self.store.build(claim)
        except ReportError as exc:
            await self.store.retry(claim, exc.code)
        except Exception:  # noqa: BLE001  # failed generation keeps its private, fixed snapshot durably retryable
            await self.store.retry(claim)
        return 1

    async def _run(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                if await self.run_once():
                    continue
            except Exception as exc:  # noqa: BLE001  # report outages must not propagate to inference or settlement
                verbose_proxy_logger.warning("Retail report generation deferred (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        try:
            await self.store.release_claims(self.owner)
        except Exception as exc:  # noqa: BLE001  # expired durable leases recover without making optional shutdown fail
            verbose_proxy_logger.warning("Retail report shutdown release deferred (%s)", type(exc).__name__)
        finally:
            await self.store.close()


class _WorkerLifecycle:
    def __init__(self) -> None:
        self.current: ReportWorker | None = None
        self.lock = asyncio.Lock()


_lifecycle: Final = _WorkerLifecycle()


async def start_report_worker(store: WalletStore) -> None:
    async with _lifecycle.lock:
        if _lifecycle.current is not None or not report_export_enabled():
            return
        selected: Final = ReportWorker(get_report_store(store))
        await selected.start()
        _lifecycle.current = selected


async def close_report_worker() -> None:
    async with _lifecycle.lock:
        selected: Final = _lifecycle.current
        _lifecycle.current = None
    if selected is not None:
        await selected.close()
