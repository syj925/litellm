import asyncio
import os
from typing import Final
from uuid import uuid4

from litellm._logging import verbose_proxy_logger

from .models import IdempotencyConflict, nanos_to_amount
from .settlement_store import (
    SettlementFact,
    claim_facts,
    finish_activity,
    finish_fact,
    release_claims,
    renew_activity,
    retry_fact,
)
from .store import WalletStore, get_store

_active: Final[dict[str, WalletStore | None]] = {}  # mutable-ok: process-owned request lease registry
_worker: "SettlementWorker | None" = None  # rebind-ok: process lifecycle replaces and clears its singleton


def register_request_activity(request_id: str, store: WalletStore | None = None) -> None:
    _active[request_id] = store


def unregister_request_activity(request_id: str) -> None:
    _active.pop(request_id, None)


async def finish_request_activity(request_id: str | None, store: WalletStore | None = None) -> None:
    if not request_id or request_id not in _active:
        return
    registered: Final = _active.pop(request_id)
    storage: Final = store or registered or get_store()
    await finish_activity(storage, request_id)


def notify_settlement() -> None:
    if _worker is not None:
        _worker.wake()


class SettlementWorker:
    def __init__(self, store: WalletStore, *, concurrency: int = 4, lease_seconds: int = 60) -> None:
        if not 1 <= concurrency <= 32 or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid settlement worker capacity or lease duration")
        self.store = store
        self.concurrency = concurrency
        self.lease_seconds = lease_seconds
        self.owner = str(uuid4())
        self._wake = asyncio.Event()
        self._closing = False
        self._task: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._overloaded = False
        self._last_pressure_check = 0.0

    def wake(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.store.initialize()
        self._task = asyncio.create_task(self._run(), name="retail-settlement-worker")
        self._heartbeat = asyncio.create_task(self._renew_active(), name="retail-active-request-leases")

    async def _process(self, fact: SettlementFact) -> None:
        if fact.amount_nanos is None:
            return
        try:
            await self.store.settle(fact.request_id, nanos_to_amount(fact.amount_nanos), dict(fact.details))
        except IdempotencyConflict:
            await finish_fact(self.store, fact, "IdempotencyConflict")
        except Exception as exc:  # noqa: BLE001  # every failed monetary attempt must remain durably retryable
            await retry_fact(self.store, fact, type(exc).__name__)
        else:
            await finish_fact(self.store, fact)

    async def run_once(self) -> int:
        facts: Final = await claim_facts(self.store, self.owner, self.concurrency, self.lease_seconds)
        if facts:
            results: Final = await asyncio.gather(*(self._process(fact) for fact in facts), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        return len(facts)

    async def _run(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                if asyncio.get_running_loop().time() - self._last_pressure_check >= 5:
                    threshold: Final = max(1, int(os.getenv("RETAIL_SETTLEMENT_MAX_BACKLOG", "1000")))
                    async with self.store.transaction() as connection:
                        cursor: Final = await connection.execute(
                            """SELECT count(*) AS total FROM (SELECT 1 FROM retail_settlement_facts
                            WHERE state IN ('pending','processing') LIMIT %s) pending""",
                            (threshold,),
                        )
                        row: Final = await cursor.fetchone()
                        self._overloaded = row is not None and row["total"] >= threshold
                    self._last_pressure_check = asyncio.get_running_loop().time()
                if await self.run_once():
                    continue
            except Exception as exc:  # noqa: BLE001  # isolate worker failures while preserving claimed facts
                self._overloaded = True
                verbose_proxy_logger.warning("Durable settlement retry deferred (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
            except TimeoutError:
                pass

    async def _renew_active(self) -> None:
        lease_seconds: Final = max(30, int(os.getenv("RETAIL_ACTIVE_LEASE_SECONDS", "120")))
        interval: Final = min(30, lease_seconds // 3)
        while not self._closing:
            try:
                owners: Final = tuple(dict.fromkeys(_active.values()))
                for owner in owners:
                    request_ids: Final = tuple(key for key, value in tuple(_active.items()) if value is owner)
                    await renew_activity(owner or self.store, request_ids, lease_seconds)
            except Exception as exc:  # noqa: BLE001  # lease renewal failure must not stop durable settlement
                verbose_proxy_logger.warning("Active request lease renewal deferred (%s)", type(exc).__name__)
            await asyncio.sleep(interval)

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        await release_claims(self.store, self.owner)


async def start_settlement_worker(store: WalletStore | None = None) -> None:
    global _worker
    if _worker is not None:
        return
    storage: Final = store or get_store()
    selected: Final = SettlementWorker(
        storage,
        concurrency=int(os.getenv("RETAIL_SETTLEMENT_CONCURRENCY", "4")),
        lease_seconds=int(os.getenv("RETAIL_SETTLEMENT_LEASE_SECONDS", "60")),
    )
    await selected.start()
    _worker = selected  # rebind-ok: publish the initialized lifecycle singleton


async def close_settlement_worker() -> None:
    global _worker
    selected: Final = _worker
    _worker = None  # rebind-ok: detach the singleton before graceful shutdown
    if selected is not None:
        await selected.close()


def settlement_is_overloaded() -> bool:
    return _worker is not None and _worker._overloaded
