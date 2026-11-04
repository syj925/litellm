import asyncio
from typing import Final
from uuid import uuid4

from litellm._logging import verbose_proxy_logger

from .control_plane_flags import (
    organization_finance_delegation_enabled,
    organization_finance_enabled,
    report_export_enabled,
)
from .delegated_report_store import DelegatedReportStore
from .organization_report_store import OrganizationReportStore
from .report_store import ReportError, ReportStore, get_report_store
from .store import WalletStore


class ReportWorker:
    def __init__(self, store: ReportStore) -> None:
        self.store = store
        self.organization_store = OrganizationReportStore(store)
        self.delegated_store = DelegatedReportStore(store)
        self._next_queue = 0
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
        await self.organization_store.expire()
        await self.delegated_store.expire()
        sources: Final[tuple[ReportStore | OrganizationReportStore | DelegatedReportStore, ...]] = (
            self.store,
            *((self.organization_store,) if organization_finance_enabled() else ()),
            *((self.delegated_store,) if organization_finance_delegation_enabled() else ()),
        )
        selected: Final = sources[self._next_queue:] + sources[:self._next_queue]
        self._next_queue = (self._next_queue + 1) % len(sources)
        for storage in selected:
            claim: Final = await storage.claim(self.owner)
            if claim is None:
                continue
            try:
                async with asyncio.timeout(20):
                    await storage.build(claim)
            except ReportError as exc:
                await storage.retry(claim, exc.code)
            except Exception:  # noqa: BLE001  # fixed private snapshots remain retryable on generation failures
                await storage.retry(claim)
            return 1
        return 0

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
        for storage in (self.store, self.organization_store, self.delegated_store):
            try:
                await storage.release_claims(self.owner)
            except Exception as exc:  # noqa: BLE001  # expired leases recover independently of optional shutdown
                verbose_proxy_logger.warning("Retail report shutdown release deferred (%s)", type(exc).__name__)
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
