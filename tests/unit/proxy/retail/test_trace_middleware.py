import asyncio
import gzip
from collections.abc import Mapping
from typing import Final

import httpx
import pytest
from pydantic import JsonValue
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

from litellm.proxy.retail.admission import RequestAdmission
from litellm.proxy.retail.model_observations import OutputDetector, _parse_payload
from litellm.proxy.retail.request_tracing import TraceCollector, bind_call_id, current_trace
from litellm.proxy.retail.trace_middleware import RequestTraceMiddleware, _Exchange
from litellm.proxy.retail.trace_writer import TraceWriter


class Capture:
    def __init__(self) -> None:
        self.rows: tuple[dict[str, JsonValue], ...] = ()
        self.bodies: tuple[JsonValue, ...] = ()

    async def __call__(self, snapshot: Mapping[str, JsonValue], body: JsonValue, request_id: str | None) -> None:
        self.rows = (*self.rows, dict(snapshot))
        self.bodies = (*self.bodies, body)


async def stream_app(scope: Scope, receive: Receive, send: Send) -> None:
    request: Final = Request(scope, receive)
    await request.body()
    bind_call_id(scope["path"])
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
    await send({"type": "http.response.body", "body": (
        b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"hmm"}}\n\n'
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
    ), "more_body": False})


@pytest.mark.asyncio
async def test_middleware_preserves_wire_data_and_redacts_captured_body() -> None:
    capture: Final = Capture()
    middleware: Final = RequestTraceMiddleware(stream_app, capture)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware), base_url="http://test") as client:
        response: Final = await client.post("/v1/messages", json={"api_key": "private", "messages": []})
    assert response.status_code == 200 and "thinking_delta" in response.text and '"text":"ok"' in response.text
    assert capture.bodies == ({"api_key": "[REDACTED]", "messages": []},)
    row: Final = capture.rows[0]
    assert response.headers["x-retail-trace-id"] == row["trace_id"]
    milestones: Final = row["milestones"]
    assert isinstance(milestones, dict)
    assert "first_model_write" in milestones and "first_text_write" in milestones
    assert row["response_status"] == 200 and current_trace() is None


@pytest.mark.asyncio
async def test_first_output_metrics_stop_decoding_after_text_but_forward_and_count_every_write() -> None:
    parsed: Final[list[str | bytes]] = []
    forwarded: Final[list[Message]] = []

    def parse(payload: str | bytes) -> Mapping[str, JsonValue]:
        parsed.append(payload)
        return _parse_payload(payload)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        forwarded.append(message)

    trace: Final = TraceCollector("POST", "/v1/messages")
    exchange: Final = _Exchange(trace, receive, send, "application/json", OutputDetector(_parse=parse))
    frames: Final = (
        b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"hmm"}}\n\n',
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"first"}}\n\n',
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"later"}}\n\n',
        b'data: {"type":"message_stop"}\n\n',
    )
    for index, frame in enumerate(frames):
        await exchange.send({"type": "http.response.body", "body": frame, "more_body": index < len(frames) - 1})
    snapshot: Final = trace.snapshot()
    milestones: Final = snapshot["milestones"]
    phases: Final = snapshot["phases"]
    assert isinstance(milestones, dict) and "first_model_write" in milestones and "first_text_write" in milestones
    assert "response_complete" in milestones
    assert isinstance(phases, list) and isinstance(phases[0], dict) and phases[0]["samples"] == len(frames)
    assert tuple(message["body"] for message in forwarded) == frames
    assert len(parsed) == 2


@pytest.mark.asyncio
async def test_concurrent_requests_keep_distinct_collector_and_call_ids() -> None:
    barrier: Final = asyncio.Barrier(2)

    async def interleaving_app(scope: Scope, receive: Receive, send: Send) -> None:
        trace: Final = current_trace()
        assert trace is not None
        await barrier.wait()
        assert current_trace() is trace
        await stream_app(scope, receive, send)

    capture: Final = Capture()
    middleware: Final = RequestTraceMiddleware(interleaving_app, capture)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware), base_url="http://test") as client:
        responses: Final = await asyncio.gather(
            client.post("/v1/messages", json={"model": "a"}),
            client.post("/v1/chat/completions", json={"model": "b"}),
        )
    assert len(capture.rows) == 2
    assert {row["call_id"] for row in capture.rows} == {"/v1/messages", "/v1/chat/completions"}
    assert responses[0].headers["x-retail-trace-id"] != responses[1].headers["x-retail-trace-id"]
    assert current_trace() is None


@pytest.mark.asyncio
async def test_slow_diagnostic_storage_does_not_hold_response_or_the_admission_slot() -> None:
    entered: Final = asyncio.Event()
    release: Final = asyncio.Event()
    capture: Final = Capture()

    async def sink(snapshot: Mapping[str, JsonValue], body: JsonValue, request_id: str | None) -> None:
        entered.set()
        await release.wait()
        await capture(snapshot, body, request_id)

    writer: Final = TraceWriter(sink, workers=1, capacity=1)
    admission: Final = RequestAdmission(1, lambda: False)
    middleware: Final = RequestTraceMiddleware(stream_app, writer=writer, admission=admission)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware), base_url="http://test") as client:
            first: Final = await client.post("/v1/messages", json={})
            assert first.status_code == 200 and admission.stats.active == 0
            await entered.wait()
            second: Final = await client.post("/v1/messages", json={})
            third: Final = await client.post("/v1/messages", json={})
        assert second.status_code == third.status_code == 200 and admission.stats.active == 0
        assert capture.rows == () and writer.stats.dropped == 1
        release.set()
        assert await writer.flush()
        assert len(capture.rows) == 2
    finally:
        release.set()
        await writer.aclose()


@pytest.mark.asyncio
async def test_full_admission_rejects_before_app_body_or_upstream_work_and_cancel_releases_slot() -> None:
    entered: Final = asyncio.Event()
    release: Final = asyncio.Event()

    async def busy(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        await release.wait()
        await stream_app(scope, receive, send)

    capture: Final = Capture()
    writer: Final = TraceWriter(capture)
    admission: Final = RequestAdmission(1, lambda: False)
    middleware: Final = RequestTraceMiddleware(busy, writer=writer, admission=admission)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware), base_url="http://test") as client:
            first: Final = asyncio.create_task(client.post("/v1/messages", json={}))
            await entered.wait()
            rejected: Final = await client.post("/v1/messages", json={"api_key": "private"})
            assert rejected.status_code == 503 and rejected.headers["retry-after"] == "1"
            assert "private" not in rejected.text and admission.stats.active == 1
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert admission.stats.active == 0
            release.set()
            next_response: Final = await client.post("/v1/messages", json={})
        assert next_response.status_code == 200 and admission.stats.active == 0
        assert await writer.flush()
        assert any(row["response_status"] == 503 for row in capture.rows)
    finally:
        release.set()
        await writer.aclose()


@pytest.mark.asyncio
async def test_request_body_limit_drops_body_but_preserves_actual_received_size() -> None:
    capture: Final = Capture()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=RequestTraceMiddleware(stream_app, capture)), base_url="http://test",
    ) as client:
        response: Final = await client.post(
            "/v1/messages", content=b"x" * 262145, headers={"content-type": "application/json"},
        )
    assert response.status_code == 200 and capture.bodies == (None,)
    assert capture.rows[0]["request_body_bytes"] == 262145
    events: Final = capture.rows[0]["events"]
    assert isinstance(events, list) and any(
        isinstance(event, dict) and event["name"] == "request_body_capture_omitted" for event in events
    )


@pytest.mark.asyncio
async def test_compressed_client_bytes_are_forwarded_without_plaintext_output_inference() -> None:
    async def compressed_app(scope: Scope, receive: Receive, send: Send) -> None:
        async def compressed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                await send({**message, "headers": [*message["headers"], (b"content-encoding", b"gzip")]})
            else:
                await send({**message, "body": gzip.compress(message["body"], mtime=0)})

        await stream_app(scope, receive, compressed_send)

    capture: Final = Capture()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=RequestTraceMiddleware(compressed_app, capture)), base_url="http://test",
    ) as client:
        response: Final = await client.post("/v1/messages", json={"model": "sample"})
    assert response.status_code == 200 and '"text":"ok"' in response.text
    milestones: Final = capture.rows[0]["milestones"]
    assert isinstance(milestones, dict) and "first_client_write" in milestones
    assert "first_model_write" not in milestones and "first_text_write" not in milestones


@pytest.mark.asyncio
async def test_failed_request_is_persisted_and_original_exception_is_preserved() -> None:
    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        bind_call_id("failure-call")
        raise ValueError("private request")

    capture: Final = Capture()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=RequestTraceMiddleware(failing, capture)), base_url="http://test",
    ) as client:
        with pytest.raises(ValueError, match="private request"):
            await client.post("/v1/messages")
    assert capture.rows[0]["response_status"] is None
    events: Final = capture.rows[0]["events"]
    assert isinstance(events, list) and len(events) == 1
    event: Final = events[0]
    assert isinstance(event, dict) and event["name"] == "request_aborted"
    assert event["attributes"] == {"error_type": "ValueError"}
    assert "private request" not in str(capture.rows)
    assert current_trace() is None
