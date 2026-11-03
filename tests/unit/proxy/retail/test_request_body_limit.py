from collections.abc import AsyncIterator
from typing import Final

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from litellm.proxy.retail.request_body_limit import (
    RequestBodyLimitMiddleware,
    _BodyLimitExceeded,
    request_body_limit_setting,
)


class Traffic:
    def __init__(self, chunks: tuple[Message, ...]) -> None:
        self._chunks = iter(chunks)
        self.reads = 0
        self.sent: tuple[Message, ...] = ()
        self.events: tuple[str, ...] = ()

    async def receive(self) -> Message:
        self.reads += 1
        self.events = (*self.events, "receive")
        return next(self._chunks)

    async def send(self, message: Message) -> None:
        self.sent = (*self.sent, message)
        self.events = (*self.events, message["type"])


class Consumer:
    def __init__(self, *, swallow_error: bool = False) -> None:
        self.calls = 0
        self.received: tuple[Message, ...] = ()
        self._swallow_error = swallow_error

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        try:
            while True:
                message: Final = await receive()
                self.received = (*self.received, message)
                if message["type"] == "http.disconnect" or not message.get("more_body", False):
                    break
        except Exception:  # noqa: BLE001  # fixture reproduces a native route translating a receive error
            if not self._swallow_error:
                raise
            await Response(b"private handler error", status_code=500)(scope, receive, send)
            return
        await Response(b"accepted")(scope, receive, send)


def _scope(
    path: str = "/v1/chat/completions", *, method: str = "POST", headers: tuple[tuple[bytes, bytes], ...] = ()
) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 1000),
        "server": ("fixture", 80),
    }


def _chunk(body: bytes, *, more: bool = False) -> Message:
    return {"type": "http.request", "body": body, "more_body": more}


@pytest.mark.asyncio
async def test_declared_oversize_is_rejected_before_body_read_or_application_entry() -> None:
    consumer: Final = Consumer()
    traffic: Final = Traffic((_chunk(b"not read"),))
    await RequestBodyLimitMiddleware(consumer, 10)(
        _scope(headers=((b"Content-Length", b"11"),)), traffic.receive, traffic.send
    )
    assert (traffic.reads, consumer.calls, traffic.sent[0]["status"]) == (0, 0, 413)
    assert b"no-store" in dict(traffic.sent[0]["headers"]).values()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ("POST", "PUT", "PATCH", "DELETE", "GET"))
async def test_actual_bytes_reject_unknown_or_understated_length_on_every_http_method(method: str) -> None:
    consumer: Final = Consumer()
    first: Final = _chunk(b"12345", more=True)
    traffic: Final = Traffic((first, _chunk(b"678901"), _chunk(b"must not be drained")))
    await RequestBodyLimitMiddleware(consumer, 10)(
        _scope("/custom/passthrough", method=method, headers=((b"content-length", b"1"),)),
        traffic.receive,
        traffic.send,
    )
    assert (traffic.reads, traffic.sent[0]["status"]) == (2, 413)
    assert consumer.received == (first,)


@pytest.mark.asyncio
async def test_chunked_request_without_content_length_is_counted_in_wire_bytes() -> None:
    consumer: Final = Consumer()
    traffic: Final = Traffic((_chunk("猫".encode(), more=True), _chunk(b"xx")))
    await RequestBodyLimitMiddleware(consumer, 4)(
        _scope(headers=((b"transfer-encoding", b"chunked"),)), traffic.receive, traffic.send
    )
    assert (traffic.reads, traffic.sent[0]["status"]) == (2, 413)
    assert len(consumer.received) == 1


@pytest.mark.asyncio
async def test_accepted_request_keeps_chunk_identity_without_extra_reads_or_buffering() -> None:
    consumer: Final = Consumer()
    chunks: Final = (_chunk(b"12345", more=True), _chunk(b"67890"))
    traffic: Final = Traffic(chunks)
    await RequestBodyLimitMiddleware(consumer, 10)(_scope(), traffic.receive, traffic.send)
    assert traffic.reads == 2 and traffic.sent[0]["status"] == 200
    assert consumer.received[0] is chunks[0] and consumer.received[1] is chunks[1]


@pytest.mark.asyncio
async def test_accepted_request_does_not_read_a_body_the_application_ignores() -> None:
    async def no_read(scope: Scope, receive: Receive, send: Send) -> None:
        await Response(b"accepted")(scope, receive, send)

    traffic: Final = Traffic(())
    await RequestBodyLimitMiddleware(no_read, 10)(
        _scope(headers=((b"content-length", b"2"),)), traffic.receive, traffic.send
    )
    assert traffic.reads == 0 and traffic.sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_downstream_error_translation_cannot_turn_known_overflow_into_500() -> None:
    traffic: Final = Traffic((_chunk(b"123456"),))
    await RequestBodyLimitMiddleware(Consumer(swallow_error=True), 5)(_scope(), traffic.receive, traffic.send)
    assert [message["status"] for message in traffic.sent if message["type"] == "http.response.start"] == [413]
    assert b"private handler error" not in b"".join(message.get("body", b"") for message in traffic.sent)


@pytest.mark.asyncio
async def test_guard_preserves_interleaved_streaming_and_response_message_identity() -> None:
    first: Final = {"type": "http.response.body", "body": b"data: first\n\n", "more_body": True}
    last: Final = {"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": False}

    async def streaming(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(first)
        await receive()
        await send(last)

    traffic: Final = Traffic((_chunk(b"123", more=True), _chunk(b"456")))
    await RequestBodyLimitMiddleware(streaming, 6)(_scope(), traffic.receive, traffic.send)
    assert traffic.events == ("receive", "http.response.start", "http.response.body", "receive", "http.response.body")
    assert traffic.sent[1] is first and traffic.sent[2] is last and traffic.reads == 2


@pytest.mark.asyncio
async def test_overflow_after_response_headers_aborts_without_sending_a_second_status() -> None:
    async def streaming(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()

    traffic: Final = Traffic((_chunk(b"too big"),))
    with pytest.raises(_BodyLimitExceeded):
        await RequestBodyLimitMiddleware(streaming, 1)(_scope(), traffic.receive, traffic.send)
    assert traffic.sent == ({"type": "http.response.start", "status": 200, "headers": []},)


@pytest.mark.asyncio
async def test_unrelated_application_failure_is_not_hidden_by_size_guard() -> None:
    async def failed(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("original failure")

    traffic: Final = Traffic(())
    with pytest.raises(RuntimeError, match="original failure"):
        await RequestBodyLimitMiddleware(failed, 1)(_scope(), traffic.receive, traffic.send)
    assert traffic.sent == () and traffic.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("value", (b"-1", b"no-number", b"", b"1,2", b"9" * 21))
async def test_malformed_content_length_is_rejected_before_any_body_read(value: bytes) -> None:
    traffic: Final = Traffic(())
    await RequestBodyLimitMiddleware(Consumer(), 10)(
        _scope(headers=((b"content-length", value),)), traffic.receive, traffic.send
    )
    assert traffic.reads == 0 and traffic.sent[0]["status"] == 400


@pytest.mark.asyncio
async def test_duplicate_content_length_is_rejected_without_selecting_a_preferred_value() -> None:
    traffic: Final = Traffic(())
    await RequestBodyLimitMiddleware(Consumer(), 10)(
        _scope(headers=((b"content-length", b"1"), (b"content-length", b"100"))), traffic.receive, traffic.send
    )
    assert traffic.reads == 0 and traffic.sent[0]["status"] == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    (
        "/chat/completions",
        "/v1/messages",
        "/v1/messages/count_tokens",
        "/v1/responses",
        "/v2/rerank",
        "/openai/deployments/model/chat/completions",
        "/openai_passthrough/v1/responses",
        "/anthropic/v1/messages",
        "/gemini/v1beta/models/m:countTokens",
        "/v1beta/models/m:generateContent",
        "/v1/models/m:streamGenerateContent",
        "/vertex_ai/v1/projects/p/locations/l/publishers/g/models/m:streamRawPredict",
    ),
)
async def test_known_provider_json_rejects_unsupported_encoding_before_body_read(path: str) -> None:
    traffic: Final = Traffic(())
    await RequestBodyLimitMiddleware(Consumer(), 10)(
        _scope(path, headers=((b"content-type", b"application/json; charset=utf-8"), (b"content-encoding", b"gzip"))),
        traffic.receive,
        traffic.send,
    )
    assert traffic.reads == 0 and traffic.sent[0]["status"] == 415


@pytest.mark.asyncio
async def test_json_encoding_scope_honors_server_root_path_without_reading_body() -> None:
    traffic: Final = Traffic(())
    scope: Final = {
        **_scope(
            "/gateway/v1/messages", headers=((b"content-type", b"application/json"), (b"content-encoding", b"gzip"))
        ),
        "root_path": "/gateway",
    }
    await RequestBodyLimitMiddleware(Consumer(), 10)(scope, traffic.receive, traffic.send)
    assert traffic.reads == 0 and traffic.sent[0]["status"] == 415


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "content_type"),
    (
        ("/audio/transcriptions", b"multipart/form-data; boundary=fixture"),
        ("/v1/files", b"application/octet-stream"),
        ("/openai/v1/audio/transcriptions", b"audio/wav"),
        ("/v1/images/edits", b"multipart/form-data; boundary=fixture"),
        ("/custom/passthrough", b"application/json"),
    ),
)
async def test_upload_and_custom_passthrough_encodings_are_preserved_under_the_byte_cap(
    path: str, content_type: bytes
) -> None:
    chunk: Final = _chunk(b"encoded")
    consumer: Final = Consumer()
    traffic: Final = Traffic((chunk,))
    await RequestBodyLimitMiddleware(consumer, 10)(
        _scope(path, headers=((b"content-type", content_type), (b"content-encoding", b"gzip"))),
        traffic.receive,
        traffic.send,
    )
    assert traffic.sent[0]["status"] == 200 and consumer.received[0] is chunk and traffic.reads == 1


@pytest.mark.asyncio
async def test_explicitly_disabled_guard_preserves_original_application_behavior() -> None:
    chunk: Final = _chunk(b"unlimited fixture")
    consumer: Final = Consumer()
    traffic: Final = Traffic((chunk,))
    await RequestBodyLimitMiddleware(consumer, 0)(
        _scope(headers=((b"content-length", b"invalid"), (b"content-encoding", b"gzip"))),
        traffic.receive,
        traffic.send,
    )
    assert traffic.sent[0]["status"] == 200 and consumer.received[0] is chunk


@pytest.mark.parametrize("value", ("-1", "1.5", "invalid", str(1024 * 1024 * 1024 + 1)))
def test_invalid_environment_limit_fails_configuration_instead_of_silently_disabling(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETAIL_MAX_REQUEST_BYTES", value)
    with pytest.raises(ValueError, match="RETAIL_MAX_REQUEST_BYTES"):
        request_body_limit_setting()


def test_request_limit_default_is_disabled_and_explicit_bytes_are_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RETAIL_MAX_REQUEST_BYTES", raising=False)
    assert request_body_limit_setting() == 0
    monkeypatch.setenv("RETAIL_MAX_REQUEST_BYTES", "33554432")
    assert request_body_limit_setting() == 32 * 1024 * 1024


class ScriptedUpstream:
    def __init__(self) -> None:
        self.requests: tuple[bytes, ...] = ()

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests = (*self.requests, request.content)
        return httpx.Response(
            200, content=b"data: accepted\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )


@pytest.mark.asyncio
async def test_http_json_overflow_never_opens_upstream_and_valid_streaming_response_is_unchanged() -> None:
    upstream: Final = ScriptedUpstream()
    app: Final = FastAPI()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream.respond)) as provider:

        @app.post("/v1/messages")
        async def endpoint(request: Request) -> Response:
            payload: Final = await request.json()
            response: Final = await provider.post("https://upstream.invalid/messages", json=payload)
            return Response(response.content, headers={"content-type": response.headers["content-type"]})

        async def oversized() -> AsyncIterator[bytes]:
            yield b'{"prompt":"'
            yield b"x" * 50 + b'"}'

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=RequestBodyLimitMiddleware(app, 40)), base_url="http://gateway"
        ) as client:
            rejected: Final = await client.post(
                "/v1/messages", content=oversized(), headers={"content-type": "application/json"}
            )
            assert rejected.status_code == 413 and upstream.requests == ()
            accepted: Final = await client.post("/v1/messages", json={"prompt": "hello"})
    assert accepted.status_code == 200 and accepted.content == b"data: accepted\n\ndata: [DONE]\n\n"
    assert len(upstream.requests) == 1
