import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Final, TypeAlias

from pydantic import JsonValue, TypeAdapter, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .request_tracing import TraceCollector, activate_trace, redact_body, reset_trace

_LOG: Final = logging.getLogger(__name__)
_JSON: Final = TypeAdapter(JsonValue)
_BODY_LIMIT: Final = 262144
_PATHS: Final = frozenset({
    "/chat/completions", "/completions", "/messages", "/responses", "/embeddings",
    "/images/generations", "/images/edits", "/audio/transcriptions", "/audio/speech",
    "/rerank", "/search", "/moderations",
})
TraceSink: TypeAlias = Callable[[Mapping[str, JsonValue], JsonValue | None, str | None], Awaitable[None]]


def traced_path(path: str) -> bool:
    normalized: Final = path.removeprefix("/v1").rstrip("/") if path.startswith("/v1/") else path.rstrip("/")
    return normalized in _PATHS or path.endswith((":generateContent", ":streamGenerateContent"))


async def _persist(snapshot: Mapping[str, JsonValue], body: JsonValue | None, request_id: str | None) -> None:
    from .trace_store import persist_trace

    await persist_trace(snapshot, request_body=body, retail_request_id=request_id)


async def _cleanup() -> None:
    from .trace_store import cleanup_trace_bodies

    while True:
        try:
            await cleanup_trace_bodies()
        except Exception as error:
            _LOG.warning("Request trace cleanup failed (%s)", type(error).__name__)
        await asyncio.sleep(60)


class _Exchange:
    def __init__(self, trace: TraceCollector, receive: Receive, send: Send, content_type: str) -> None:
        from .model_observations import OutputDetector

        self.trace = trace
        self._receive = receive
        self._send = send
        self._content_type = content_type
        self._body = b""
        self._body_complete = False
        self._capture_omitted = False
        self._output = OutputDetector()
        self._observable = True

    async def receive(self) -> Message:
        message: Final = await self._receive()
        if message["type"] == "http.disconnect":
            self.trace.event("client_disconnected")
        if message["type"] != "http.request":
            return message
        body: Final = message.get("body", b"")
        self.trace.request_body_bytes += len(body)
        if len(self._body) + len(body) <= _BODY_LIMIT and not self._capture_omitted:
            self._body += body
        else:
            self._body = b""
            self._capture_omitted = True
        if not message.get("more_body", False):
            self._body_complete = True
            self.trace.milestone("request_body_complete")
            self.trace.event("request_body_received", {"size_bytes": self.trace.request_body_bytes})
        return message

    def captured_body(self) -> JsonValue | None:
        if self._capture_omitted:
            self.trace.event("request_body_capture_omitted", {"reason": "size_limit", "limit_bytes": _BODY_LIMIT})
            return None
        if not self._body_complete or "json" not in self._content_type or not self._body:
            return None
        try:
            return redact_body(_JSON.validate_json(self._body))
        except (ValidationError, ValueError):
            self.trace.event("request_body_capture_omitted", {"reason": "invalid_json"})
            return None

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.trace.response_status = message["status"]
            self.trace.milestone("response_headers")
            headers: Final = message.get("headers", [])
            encoding: Final = next((
                value.decode("latin-1").lower()
                for key, value in headers if key.lower() == b"content-encoding"
            ), "identity")
            self._observable = encoding in {"", "identity"}
            call_id: Final = next((
                value.decode("latin-1") for key, value in headers if key.lower() == b"x-litellm-call-id"
            ), None)
            if call_id and self.trace.call_id is None:
                self.trace.call_id = call_id[:191]
            await self._send({**message, "headers": [
                *headers, (b"x-retail-trace-id", self.trace.trace_id.encode("ascii")),
            ]})
            return
        if message["type"] != "http.response.body":
            await self._send(message)
            return
        body: Final = message.get("body", b"")
        signals: Final = self._output.observe_signals(body) if self._observable else ()
        started: Final = self.trace.offset_ms()
        await self._send(message)
        self.trace.phase("client.write", started, "ok")
        if body:
            self.trace.milestone("first_client_write")
        for signal in signals:
            self.trace.milestone("first_model_write")
            if signal.kind == "text":
                self.trace.milestone("first_text_write")
        if not message.get("more_body", False):
            self.trace.milestone("response_complete")


class RequestTraceMiddleware:
    def __init__(self, app: ASGIApp, sink: TraceSink = _persist) -> None:
        self.app = app
        self._sink = sink
        self._cleanup_task: asyncio.Task[None] | None = None

    async def _lifespan_send(self, send: Send, message: Message) -> None:
        if message["type"] == "lifespan.startup.complete" and self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(_cleanup())
        if message["type"] == "lifespan.shutdown.complete" and self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._cleanup_task
        await send(message)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            async def lifespan_send(message: Message) -> None:
                await self._lifespan_send(send, message)

            await self.app(scope, receive, lifespan_send)
            return
        if scope["type"] != "http" or scope["method"] != "POST" or not traced_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        trace: Final = TraceCollector(scope["method"], scope["path"])
        client: Final = scope.get("client")
        trace.set_attributes({
            "client_ip": client[0] if client else None,
            "inbound_protocol": "anthropic" if scope["path"].endswith("/messages") else
            "gemini" if ":generateContent" in scope["path"] or ":streamGenerateContent" in scope["path"] else
            "openai_responses" if scope["path"].endswith("/responses") else
            "openai_chat" if scope["path"].endswith("/chat/completions") else "openai",
        })
        content_type: Final = next((
            value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == b"content-type"
        ), "")
        exchange: Final = _Exchange(trace, receive, send, content_type)
        token: Final = activate_trace(trace)
        try:
            await self.app(scope, exchange.receive, exchange.send)
        except BaseException as error:
            trace.event("request_aborted", {"error_type": type(error).__name__})
            raise
        finally:
            body: Final = exchange.captured_body()
            trace.complete()
            try:
                await asyncio.wait_for(self._sink(trace.snapshot(), body, trace.retail_request_id), timeout=3)
            except Exception as error:
                _LOG.warning("Request trace persistence failed (%s)", type(error).__name__)
            finally:
                reset_trace(token)
