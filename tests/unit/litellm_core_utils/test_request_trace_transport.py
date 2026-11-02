import asyncio
import gzip
import json
from collections.abc import AsyncIterator, Mapping
from typing import Final

import httpx
import pytest

from litellm.litellm_core_utils.request_trace_transport import merge_trace_hooks, routing_trace_scope
from litellm.proxy.retail.request_tracing import TraceCollector, activate_trace, reset_trace


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.closed = True


class _Seen:
    def __init__(self) -> None:
        self.events: tuple[str, ...] = ()

    async def request(self, request: httpx.Request) -> None:
        self.events = (*self.events, "request")

        async def existing_trace(name: str, info: Mapping[str, object]) -> None:
            self.events = (*self.events, name)

        request.extensions["trace"] = existing_trace

    async def response(self, response: httpx.Response) -> None:
        self.events = (*self.events, "response")


class _HTTPcoreReadFailure(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"private output"}}]}\n\n'
        error: Final = httpx.ReadError("private read failure", request=self._request)
        await self._request.extensions["trace"]("http11.receive_response_body.failed", {"exception": error})
        raise error


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _attempts(trace: TraceCollector) -> tuple[Mapping[str, object], ...]:
    rows: Final = trace.snapshot()["attempts"]
    assert isinstance(rows, list)
    return tuple(_mapping(row) for row in rows)


def _events(attempt: Mapping[str, object]) -> tuple[str, ...]:
    rows: Final = attempt["events"]
    assert isinstance(rows, list)
    return tuple(str(_mapping(row)["name"]) for row in rows)


@pytest.mark.asyncio
async def test_inactive_tracing_keeps_existing_hooks_and_body_without_collecting_attempts() -> None:
    seen: Final = _Seen()
    chunks: Final = (b"first", b"second")

    async def server(request: httpx.Request) -> httpx.Response:
        assert "litellm_request_trace_attempt" not in request.extensions
        return httpx.Response(200, stream=_Chunks(chunks))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server),
        event_hooks=merge_trace_hooks(
            {
                "request": [seen.request],
                "response": [seen.response],
            }
        ),
    ) as client:
        async with client.stream("POST", "https://fixture.invalid/v1", content=b"private prompt") as response:
            result: Final = tuple([chunk async for chunk in response.aiter_raw()])
    assert result == chunks
    assert seen.events == ("request", "response")


@pytest.mark.asyncio
async def test_actual_stream_events_preserve_all_outputs_and_bytes_without_persisting_content() -> None:
    trace: Final = TraceCollector("POST", "/v1/messages")
    token: Final = activate_trace(trace)
    seen: Final = _Seen()
    chunks: Final = (
        b": keepalive\n\n",
        b'event: message_start\ndata: {"type":"message_start","message":{"role":"assistant"}}\n\n',
        b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"private reasoning"}}\n\n'
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"private visible output"}}\n\n',
    )

    async def server(request: httpx.Request) -> httpx.Response:
        callback: Final = request.extensions["trace"]
        await callback("http11.send_request_headers.started", {})
        await callback("http11.send_request_headers.complete", {})
        return httpx.Response(
            200,
            headers={"x-request-id": "upstream-fixture", "content-type": "text/event-stream"},
            stream=_Chunks(chunks),
            extensions={"http_version": b"HTTP/2"},
        )

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(server),
            event_hooks=merge_trace_hooks(
                {
                    "request": [seen.request],
                    "response": [seen.response],
                }
            ),
        ) as client:
            with routing_trace_scope(
                {
                    "model_name": "public-fixture",
                    "model_info": {"id": "deployment-fixture"},
                    "litellm_params": {
                        "model": "anthropic/fixture",
                        "api_base": "https://user:password@fixture.invalid/base?token=secret",
                    },
                },
                {},
            ):
                async with client.stream("POST", "https://fixture.invalid/v1/messages?secret=hidden") as response:
                    result: Final = tuple([chunk async for chunk in response.aiter_raw()])
        attempt: Final = _attempts(trace)[0]
        attributes: Final = _mapping(attempt["attributes"])
        milestones: Final = _mapping(trace.snapshot()["milestones"])
        assert result == chunks
        assert (
            attributes["deployment_id"] == "deployment-fixture"
            and attributes["api_base_origin"] == "https://fixture.invalid"
        )
        assert attributes["http_version"] == "HTTP/2"
        assert attempt["status_code"] == 200 and attempt["upstream_request_id"] == "upstream-fixture"
        assert attempt["ended_ms"] is not None
        assert "first_reasoning_output" in milestones and "first_text_output" in milestones
        assert milestones["upstream_first_event"] < milestones["first_model_output"]
        assert "first_reasoning_output" in _events(attempt) and "first_text_output" in _events(attempt)
        assert not any("connect_tcp" in name or "start_tls" in name for name in _events(attempt))
        snapshot: Final = json.dumps(trace.snapshot())
        assert all(
            private not in snapshot
            for private in ("private reasoning", "private visible output", "password", "secret=hidden")
        )
        assert seen.events == (
            "request",
            "http11.send_request_headers.started",
            "http11.send_request_headers.complete",
            "response",
        )
    finally:
        reset_trace(token)


@pytest.mark.asyncio
async def test_response_error_request_id_is_extracted_without_saving_error_body_or_secrets() -> None:
    trace: Final = TraceCollector("POST", "/v1/chat/completions")
    token: Final = activate_trace(trace)

    async def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error": {"message": "private error; request id: req_from_body", "api_key": "private-key"}}
        )

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server), event_hooks=merge_trace_hooks()) as client:
            await client.post("https://fixture.invalid/v1")
        attempt: Final = _attempts(trace)[0]
        assert attempt["status_code"] == 429 and attempt["error_type"] == "HTTPStatusError"
        assert attempt["upstream_request_id"] == "req_from_body"
        snapshot: Final = json.dumps(trace.snapshot())
        assert "private error" not in snapshot and "private-key" not in snapshot
        assert "first_model_output" not in _mapping(trace.snapshot()["milestones"])
    finally:
        reset_trace(token)


@pytest.mark.asyncio
async def test_connection_failure_without_a_response_is_finished_and_retry_scope_does_not_leak() -> None:
    trace: Final = TraceCollector("POST", "/v1/chat/completions")
    token: Final = activate_trace(trace)

    async def server(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fail":
            raise httpx.ConnectError("private connection error", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "private result"}}]})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server), event_hooks=merge_trace_hooks()) as client:
            with pytest.raises(httpx.ConnectError):
                with routing_trace_scope({"model_info": {"id": "failed"}}, {}):
                    await client.post("https://fixture.invalid/fail")
            with routing_trace_scope({"model_info": {"id": "retry"}}, {"metadata": {"attempted_retries": 1}}):
                await client.post("https://fixture.invalid/success")
        first, second = _attempts(trace)
        assert first["ended_ms"] is not None and first["error_type"] == "ConnectError" and first["status_code"] is None
        assert second["status_code"] == 200 and second["error_type"] is None
        assert _mapping(first["attributes"])["deployment_id"] == "failed"
        assert _mapping(second["attributes"])["deployment_id"] == "retry"
        assert _mapping(second["attributes"])["retry_index"] == 1
        assert "private connection error" not in json.dumps(trace.snapshot())
    finally:
        reset_trace(token)


@pytest.mark.asyncio
async def test_compressed_raw_bytes_are_not_mistaken_for_sse_and_read_failure_is_preserved() -> None:
    trace: Final = TraceCollector("POST", "/v1/messages")
    token: Final = activate_trace(trace)
    compressed: Final = gzip.compress(b'data: {"choices":[{"delta":{"content":"private output"}}]}\n\n')

    async def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Chunks((compressed,)))

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server), event_hooks=merge_trace_hooks()) as client:
            async with client.stream("POST", "https://fixture.invalid/v1") as response:
                raw: Final = b"".join([chunk async for chunk in response.aiter_raw()])
        assert raw == compressed
        assert "first_model_output" not in _mapping(trace.snapshot()["milestones"])
        assert _mapping(_attempts(trace)[0]["attributes"])["content_encoding"] == "gzip"
    finally:
        reset_trace(token)


@pytest.mark.asyncio
async def test_body_read_failure_has_a_terminal_attempt_without_reclassifying_it_as_success() -> None:
    trace: Final = TraceCollector("POST", "/v1/messages")
    token: Final = activate_trace(trace)

    async def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_Chunks(
                (b'data: {"choices":[{"delta":{"reasoning":"private"}}]}\n\n',),
                httpx.ReadError("private read error", request=request),
            ),
        )

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server), event_hooks=merge_trace_hooks()) as client:
            with pytest.raises(httpx.ReadError):
                await client.post("https://fixture.invalid/v1")
        attempt: Final = _attempts(trace)[0]
        assert (
            attempt["status_code"] == 200 and attempt["error_type"] == "ReadError" and attempt["ended_ms"] is not None
        )
        assert "response_body.failed" in _events(attempt)
        assert "private read error" not in json.dumps(trace.snapshot())
    finally:
        reset_trace(token)


@pytest.mark.asyncio
async def test_httpcore_read_failure_preserves_received_status_and_header_id() -> None:
    trace: Final = TraceCollector("POST", "/v1/chat/completions")
    token: Final = activate_trace(trace)

    async def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-request-id": "req_read_failed"}, stream=_HTTPcoreReadFailure(request))

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server), event_hooks=merge_trace_hooks()) as client:
            with pytest.raises(httpx.ReadError):
                await client.post("https://fixture.invalid/v1/chat/completions")
        attempt: Final = _attempts(trace)[0]
        assert attempt["status_code"] == 200 and attempt["upstream_request_id"] == "req_read_failed"
        assert attempt["ended_ms"] is not None and attempt["error_type"] == "ReadError"
        assert "http11.receive_response_body.failed" in _events(attempt)
    finally:
        reset_trace(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal",
    (
        b"data: [DONE]\n\n",
        b'data: {"type":"message_stop"}\n\n',
        b'data: {"type":"response.completed","response":{"output":[]}}\n\n',
    ),
)
async def test_consumer_close_after_terminal_is_success_without_inventing_socket_eof(terminal: bytes) -> None:
    trace: Final = TraceCollector("POST", "/v1/chat/completions")
    token: Final = activate_trace(trace)

    async def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Chunks((terminal, b"unconsumed data")))

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server), event_hooks=merge_trace_hooks()) as client:
            async with client.stream("POST", "https://fixture.invalid/v1/chat/completions") as response:
                iterator: Final = response.aiter_raw()
                assert await iterator.__anext__() == terminal
                await response.aclose()
                await iterator.aclose()
        attempt: Final = _attempts(trace)[0]
        assert attempt["status_code"] == 200 and attempt["error_type"] is None
        assert "response_body.consumer_closed_after_terminal" in _events(attempt)
        assert "response_body.complete" not in _events(attempt) and "response_body.failed" not in _events(attempt)
    finally:
        reset_trace(token)


@pytest.mark.asyncio
async def test_consumer_close_before_terminal_is_explicitly_incomplete() -> None:
    trace: Final = TraceCollector("POST", "/v1/chat/completions")
    token: Final = activate_trace(trace)

    async def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Chunks((b": keepalive\n\n", b"unconsumed data")))

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server), event_hooks=merge_trace_hooks()) as client:
            async with client.stream("POST", "https://fixture.invalid/v1/chat/completions") as response:
                iterator: Final = response.aiter_raw()
                await iterator.__anext__()
                await response.aclose()
                await iterator.aclose()
        assert _attempts(trace)[0]["error_type"] == "StreamClosedBeforeCompletion"
    finally:
        reset_trace(token)


@pytest.mark.asyncio
async def test_native_router_openai_calls_keep_task_local_deployment_identity_at_http_boundary() -> None:
    from openai import AsyncOpenAI

    from litellm import Router

    trace: Final = TraceCollector("POST", "/v1/chat/completions")
    token: Final = activate_trace(trace)
    barrier: Final = asyncio.Barrier(2)

    async def server(request: httpx.Request) -> httpx.Response:
        payload: Final = json.loads(request.content)
        await barrier.wait()
        return httpx.Response(
            200,
            json={
                "id": f"response-{payload['model']}",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server), event_hooks=merge_trace_hooks()) as client:
        sdk: Final = AsyncOpenAI(api_key="fixture-key", base_url="https://fixture.invalid/v1", http_client=client)
        router: Final = Router(
            model_list=[
                {
                    "model_name": group,
                    "model_info": {"id": f"deployment-{group}"},
                    "litellm_params": {"model": f"openai/{group}", "api_key": "fixture-key"},
                }
                for group in ("first", "second")
            ],
            num_retries=0,
        )
        try:
            responses: Final = await asyncio.gather(
                *(
                    router.acompletion(model=group, messages=[{"role": "user", "content": "fixture"}], client=sdk)
                    for group in ("first", "second")
                )
            )
            assert all(response.choices[0].message.content == "ok" for response in responses)
            attempts: Final = _attempts(trace)
            assert len(attempts) == 2
            identities: Final = {
                (_mapping(row["attributes"])["deployment_id"], _mapping(row["attributes"])["model"]) for row in attempts
            }
            assert identities == {("deployment-first", "openai/first"), ("deployment-second", "openai/second")}
            assert all(row["status_code"] == 200 and row["ended_ms"] is not None for row in attempts)
        finally:
            reset_trace(token)
            await sdk.close()
