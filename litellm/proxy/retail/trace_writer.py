import asyncio
import contextvars
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, TypeAlias

from pydantic import BaseModel, ConfigDict, JsonValue

from .trace_store import _body_value, redact_trace_value

TraceSink: TypeAlias = Callable[[Mapping[str, JsonValue], JsonValue | None, str | None], Awaitable[None]]
_LOG: Final = logging.getLogger(__name__)
_MAX_JOB_BYTES: Final = 327_680


class _Write(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot: Mapping[str, JsonValue]
    request_body: JsonValue = None
    retail_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class TraceWriterStats:
    queued: int
    active: int
    accepted: int
    coalesced: int
    dropped: int
    failed: int


def _duration(snapshot: Mapping[str, JsonValue]) -> float:
    value: Final = snapshot.get("duration_ms")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _identity(snapshot: Mapping[str, JsonValue]) -> str | None:
    value: Final = snapshot.get("trace_id") or snapshot.get("call_id")
    return value if isinstance(value, str) and 0 < len(value) <= 191 else None


def _merge(earlier: _Write, later: _Write) -> _Write:
    selected: Final = later.snapshot if _duration(later.snapshot) >= _duration(earlier.snapshot) else earlier.snapshot
    return _Write(
        snapshot=selected,
        request_body=earlier.request_body if earlier.request_body is not None else later.request_body,
        retail_request_id=later.retail_request_id or earlier.retail_request_id,
    )


async def _persist(snapshot: Mapping[str, JsonValue], body: JsonValue, request_id: str | None) -> None:
    from .trace_store import persist_trace

    await persist_trace(snapshot, body, request_id)


class TraceWriter:
    def __init__(self, sink: TraceSink = _persist, *, capacity: int = 256, workers: int = 2, timeout: float = 2) -> None:
        if capacity < 1 or workers < 1 or timeout <= 0:
            raise ValueError("Trace writer limits must be positive")
        self._sink = sink
        self._capacity = capacity
        self._workers = workers
        self._timeout = timeout
        self._pending = OrderedDict[str, bytes]()
        self._ready = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._tasks: tuple[asyncio.Task[None], ...] = ()
        self._closed = False
        self._active = 0
        self._accepted = 0
        self._coalesced = 0
        self._dropped = 0
        self._failed = 0

    @property
    def stats(self) -> TraceWriterStats:
        return TraceWriterStats(
            len(self._pending), self._active, self._accepted, self._coalesced, self._dropped, self._failed,
        )

    def _drop(self) -> bool:
        self._dropped += 1
        if self._dropped & (self._dropped - 1) == 0:
            _LOG.warning("Request trace queue rejected a diagnostic record (count=%s)", self._dropped)
        return False

    def submit(
        self, snapshot: Mapping[str, JsonValue], request_body: JsonValue = None, retail_request_id: str | None = None,
    ) -> bool:
        identity: Final = _identity(snapshot)
        if self._closed or identity is None or (identity not in self._pending and len(self._pending) >= self._capacity):
            return self._drop()
        redacted: Final = redact_trace_value(dict(snapshot))
        if not isinstance(redacted, dict):
            return self._drop()
        incoming: Final = _Write(
            snapshot=redacted, request_body=_body_value(request_body), retail_request_id=retail_request_id,
        )
        previous: Final = self._pending.get(identity)
        merged: Final = _merge(_Write.model_validate_json(previous), incoming) if previous is not None else incoming
        encoded: Final = merged.model_dump_json().encode("utf-8")
        if len(encoded) > _MAX_JOB_BYTES:
            return self._drop()
        self._pending[identity] = encoded
        self._accepted += 1
        if previous is not None:
            self._coalesced += 1
        self._idle.clear()
        self._ready.set()
        if not self._tasks:
            self._tasks = tuple(
                contextvars.Context().run(asyncio.create_task, self._run()) for _ in range(self._workers)
            )
        return True

    async def _run(self) -> None:
        while True:
            await self._ready.wait()
            if not self._pending:
                self._ready.clear()
                continue
            _, encoded = self._pending.popitem(last=False)
            self._active += 1
            if not self._pending:
                self._ready.clear()
            try:
                job: Final = _Write.model_validate_json(encoded)
                await asyncio.wait_for(
                    self._sink(job.snapshot, job.request_body, job.retail_request_id), timeout=self._timeout,
                )
            except Exception as error:
                self._failed += 1
                _LOG.warning("Request trace write failed (%s)", type(error).__name__)
            finally:
                self._active -= 1
                if not self._pending and not self._active:
                    self._idle.set()

    async def flush(self, timeout: float = 5) -> bool:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def aclose(self, timeout: float = 5) -> bool:
        self._closed = True
        flushed: Final = await self.flush(timeout)
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        if self._pending:
            self._dropped += len(self._pending)
            _LOG.warning("Request trace shutdown left %s diagnostic records unwritten", len(self._pending))
        self._pending.clear()
        return flushed


class _WriterState:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.writer: TraceWriter | None = None


_STATE: Final = _WriterState()


def get_trace_writer() -> TraceWriter:
    loop: Final = asyncio.get_running_loop()
    if _STATE.loop is not loop or _STATE.writer is None:
        _STATE.loop = loop
        _STATE.writer = TraceWriter()
    return _STATE.writer


def queue_trace(
    snapshot: Mapping[str, JsonValue], request_body: JsonValue = None, retail_request_id: str | None = None,
) -> bool:
    return get_trace_writer().submit(snapshot, request_body, retail_request_id)


async def flush_traces(timeout: float = 5) -> bool:
    return await get_trace_writer().flush(timeout)


async def close_traces(timeout: float = 5) -> bool:
    return await get_trace_writer().aclose(timeout)
