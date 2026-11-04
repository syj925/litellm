import asyncio
from typing import Final
from uuid import uuid4

from litellm._logging import verbose_proxy_logger

from .control_plane_flags import native_audit_enabled
from .native_audit_store import NativeAuditClient, NativeAuditConflict, NativeAuditStore
from .store import WalletStore


class NativeAuditWorker:
    def __init__(self, store: NativeAuditStore) -> None:
        self.store = store
        self.owner = str(uuid4())
        self._closing = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.store.initialize()
        self._task = asyncio.create_task(self._run(),name="retail-native-audit-worker")

    async def run_once(self) -> int:
        if not native_audit_enabled():
            return 0
        await self.store.maintain()
        claim: Final = await self.store.claim(self.owner)
        if claim is None:
            return 0
        try:
            async with asyncio.timeout(8):
                await self.store.bridge(claim)
                await self.store.acknowledge(claim)
        except NativeAuditConflict:
            await self.store.fail(claim,"integrity_conflict")
        except Exception:  # noqa: BLE001  # uncertain cross-database commits must replay the same immutable event
            await self.store.fail(claim)
        return 1

    async def _run(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                if await self.run_once():
                    continue
            except Exception as exc:  # noqa: BLE001  # optional audit reconciliation must never block model traffic
                verbose_proxy_logger.warning("Native audit reconciliation deferred (%s)",type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(),timeout=1)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task,timeout=5)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task,return_exceptions=True)
        try:
            await self.store.release_claims(self.owner)
        except Exception as exc:  # noqa: BLE001  # durable leases survive unavailable native storage during shutdown
            verbose_proxy_logger.warning("Native audit shutdown deferred (%s)",type(exc).__name__)
        finally:
            await self.store.close()


class _Lifecycle:
    def __init__(self) -> None:
        self.current: NativeAuditWorker | None = None
        self.lock = asyncio.Lock()


_lifecycle: Final = _Lifecycle()


async def start_native_audit_worker(native_client: NativeAuditClient, store: WalletStore) -> None:
    async with _lifecycle.lock:
        if _lifecycle.current is not None or not native_audit_enabled():
            return
        selected: Final = NativeAuditWorker(NativeAuditStore(native_client,store.database_url))
        try:
            await selected.start()
        except Exception:  # noqa: BLE001  # failed optional startup must close only the resources it allocated
            await selected.store.close()
            raise
        _lifecycle.current = selected


async def close_native_audit_worker() -> None:
    async with _lifecycle.lock:
        selected: Final = _lifecycle.current
        _lifecycle.current = None
    if selected is not None:
        await selected.close()
