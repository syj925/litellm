import asyncio
from collections.abc import Mapping
from typing import Final

import pytest
from pydantic import JsonValue

from litellm.proxy.retail.request_tracing import current_trace
from litellm.proxy.retail.trace_writer import TraceWriter


class _Capture:
    def __init__(self) -> None:
        self.rows: tuple[tuple[Mapping[str, JsonValue], JsonValue, str | None], ...] = ()

    async def __call__(self, snapshot: Mapping[str, JsonValue], body: JsonValue, request_id: str | None) -> None:
        assert current_trace() is None
        self.rows = (*self.rows, (snapshot, body, request_id))


@pytest.mark.asyncio
async def test_queued_updates_keep_newer_snapshot_and_first_redacted_body_without_extending_start() -> None:
    capture: Final = _Capture()
    writer: Final = TraceWriter(capture, workers=1)
    identity: Final = {"trace_id": "same-server-id", "started_at": "2026-01-01T00:00:00Z"}
    try:
        assert writer.submit({**identity, "duration_ms": 20}, None, "reservation")
        assert writer.submit({**identity, "duration_ms": 10}, {"api_key": "private", "messages": ["first"]})
        assert writer.submit({**identity, "duration_ms": 15}, {"messages": ["must not replace"]})
        assert await writer.flush()
        assert len(capture.rows) == 1
        snapshot, body, request_id = capture.rows[0]
        assert snapshot == {**identity, "duration_ms": 20}
        assert body == {"api_key": "[REDACTED]", "messages": ["first"]}
        assert request_id == "reservation" and writer.stats.coalesced == 2
    finally:
        assert await writer.aclose()


@pytest.mark.asyncio
async def test_writer_capacity_and_execution_remain_bounded_and_failures_do_not_block_later_records() -> None:
    entered: Final = asyncio.Event()
    release: Final = asyncio.Event()
    capture: Final = _Capture()

    async def sink(snapshot: Mapping[str, JsonValue], body: JsonValue, request_id: str | None) -> None:
        if snapshot["trace_id"] == "blocked":
            entered.set()
            await release.wait()
            raise RuntimeError("fixture failure")
        await capture(snapshot, body, request_id)

    writer: Final = TraceWriter(sink, workers=1, capacity=1)
    try:
        assert writer.submit({"trace_id": "blocked"})
        await entered.wait()
        assert writer.submit({"trace_id": "queued"})
        assert not writer.submit({"trace_id": "overflow"})
        assert writer.stats.active == 1 and writer.stats.queued == 1 and writer.stats.dropped == 1
        release.set()
        assert await writer.flush()
        assert capture.rows[0][0]["trace_id"] == "queued"
        assert len(capture.rows) == 1 and writer.stats.failed == 1
    finally:
        release.set()
        await writer.aclose()


@pytest.mark.asyncio
async def test_oversized_diagnostics_are_rejected_and_closed_writer_does_not_restart_tasks() -> None:
    capture: Final = _Capture()
    writer: Final = TraceWriter(capture)
    oversized: Final = {"trace_id": "oversized", "attempts": [{"attribute": "x" * 16000} for _ in range(25)]}
    assert not writer.submit(oversized)
    assert await writer.aclose()
    assert not writer.submit({"trace_id": "after-close"})
    assert capture.rows == () and writer.stats.dropped == 2
