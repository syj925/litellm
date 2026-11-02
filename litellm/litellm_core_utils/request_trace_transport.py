from __future__ import annotations

import inspect
import json
import re
import sys
from collections.abc import AsyncIterator, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import httpx

if TYPE_CHECKING:
    from litellm.proxy.retail.request_tracing import AttemptCollector, TraceCollector

_ROUTING: Final[ContextVar[Mapping[str, object]]] = ContextVar("retail_trace_routing", default=MappingProxyType({}))
_HTTP_ATTEMPT: Final[ContextVar[AttemptCollector | None]] = ContextVar("retail_trace_http_attempt", default=None)
_ATTEMPT_EXTENSION: Final = "litellm_request_trace_attempt"
_MAX_ERROR_BYTES: Final = 65_536
_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_MESSAGE_ID: Final = re.compile(
    r"(?:request[ _-]?id|trace[ _-]?id)\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._:/-]{0,127})", re.I
)


def _trace() -> TraceCollector | None:
    if "litellm.proxy.retail.request_tracing" not in sys.modules:
        return None
    from litellm.proxy.retail.request_tracing import current_trace

    return current_trace()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _origin(value: object) -> str | None:
    if not isinstance(value, (str, httpx.URL)):
        return None
    try:
        url: Final = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host:
            return None
        host: Final = f"[{url.host}]" if ":" in url.host else url.host
        return f"{url.scheme}://{host}" + (f":{url.port}" if url.port else "")
    except (ValueError, httpx.InvalidURL):
        return None


def record_trace_event(name: str, attributes: Mapping[str, object]) -> None:
    trace: Final = _trace()
    if trace is not None:
        trace.event(name, attributes)


@contextmanager
def trace_router_phase(name: str) -> Generator[None]:
    trace: Final = _trace()
    if trace is None:
        yield None
        return
    started: Final = trace.offset_ms()
    try:
        yield None
    except BaseException:
        trace.phase(name, started, "error")
        raise
    else:
        trace.phase(name, started, "ok")


@contextmanager
def routing_trace_scope(deployment: Mapping[str, object], kwargs: Mapping[str, object]) -> Generator[None]:
    trace: Final = _trace()
    if trace is None:
        yield None
        return
    params: Final = _mapping(deployment.get("litellm_params"))
    info: Final = _mapping(deployment.get("model_info"))
    metadata: Final = _mapping(kwargs.get("litellm_metadata") or kwargs.get("metadata"))
    model: Final = params.get("model")
    provider: Final = params.get("custom_llm_provider") or (model.partition("/")[0] if isinstance(model, str) else None)
    attributes: Final = {
        "deployment_id": info.get("id"),
        "model": model,
        "model_group": deployment.get("model_name"),
        "provider": provider,
        "api_base_origin": _origin(params.get("api_base")),
        "retry_index": metadata.get("attempted_retries"),
        "fallback_index": metadata.get("attempted_fallbacks"),
    }
    token: Final = _ROUTING.set(MappingProxyType(attributes))
    attempt_token: Final = _HTTP_ATTEMPT.set(None)
    trace.event("deployment_selected", attributes)
    try:
        yield None
    except BaseException as exc:
        upstream_id: Final = _request_id(getattr(exc, "request_id", None))
        trace.event("deployment_call_failed", {"error_type": type(exc).__name__, "upstream_request_id": upstream_id})
        _finish(_HTTP_ATTEMPT.get(), error_type=type(exc).__name__, upstream_request_id=upstream_id)
        raise
    finally:
        _ROUTING.reset(token)
        _HTTP_ATTEMPT.reset(attempt_token)


def _finish(
    attempt: AttemptCollector | None,
    status_code: int | None = None,
    error_type: str | None = None,
    upstream_request_id: str | None = None,
) -> None:
    if attempt is not None and attempt.snapshot().get("ended_ms") is None:
        attempt.finish(status_code=status_code, error_type=error_type, upstream_request_id=upstream_request_id)


def _request_id(value: object) -> str | None:
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None else None


def _header_id(headers: httpx.Headers) -> str | None:
    return next(
        (
            value
            for key in ("x-request-id", "request-id", "x-amzn-requestid", "x-amz-request-id")
            if (value := _request_id(headers.get(key))) is not None
        ),
        None,
    )


def _body_id(content: bytes) -> str | None:
    try:
        payload: Final[object] = json.loads(content)
    except (ValueError, UnicodeError):
        return None
    document: Final = _mapping(payload)
    sources: Final = (document, _mapping(document.get("error")))
    direct: Final = next(
        (
            value
            for source in sources
            if (
                value := next(
                    (
                        _request_id(source.get(key))
                        for key in ("request_id", "requestId", "request-id")
                        if _request_id(source.get(key)) is not None
                    ),
                    None,
                )
            )
            is not None
        ),
        None,
    )
    if direct is not None:
        return direct
    message: Final = _mapping(document.get("error")).get("message")
    found: Final = _MESSAGE_ID.search(message[:4096]) if isinstance(message, str) else None
    return _request_id(found.group(1)) if found is not None else None


def _upstream_protocol(path: str) -> str | None:
    if path.rstrip("/").endswith("/messages"):
        return "anthropic"
    if path.rstrip("/").endswith("/chat/completions"):
        return "openai_chat"
    if path.rstrip("/").endswith("/responses"):
        return "openai_responses"
    if path.endswith((":generateContent", ":streamGenerateContent")):
        return "gemini"
    return None


async def trace_request(request: httpx.Request) -> None:
    trace: Final = _trace()
    if trace is None:
        return
    protocol: Final = _upstream_protocol(request.url.path)
    route: Final = {"upstream_protocol": protocol, "upstream_path": request.url.path[:512]}
    trace.set_attributes(route)
    trace.event("protocol_route", route)
    previous: Final = _HTTP_ATTEMPT.get()
    if previous is not None and previous.snapshot().get("ended_ms") is None:
        previous.event("next_attempt_started_without_response", {})
        previous.finish()
    attempt: Final = trace.start_attempt(
        {
            **_ROUTING.get(),
            **route,
            "method": request.method,
            "api_base_origin": _origin(request.url),
        }
    )
    if attempt is None:
        return
    _HTTP_ATTEMPT.set(attempt)
    request.extensions[_ATTEMPT_EXTENSION] = attempt  # rebind-ok: HTTPX hooks attach per-request observation state
    prior_trace: Final = request.extensions.get("trace")

    async def httpcore_trace(name: str, info: Mapping[str, object]) -> None:
        if callable(prior_trace):
            previous_result: Final[object] = prior_trace(name, info)
            if inspect.isawaitable(previous_result):
                await previous_result
        await attempt.trace_httpx(name, info)
        if name.endswith(".failed") and not name.endswith(("receive_response_body.failed", "response_closed.failed")):
            error: Final = info.get("exception")
            _finish(attempt, error_type=type(error).__name__ if error is not None else None)

    request.extensions["trace"] = httpcore_trace  # rebind-ok: HTTPcore reads the trace callback from request extensions
    attempt.event("http_request_started", {})


class _TracedResponseStream(httpx.AsyncByteStream):
    def __init__(
        self, stream: httpx.AsyncByteStream, response: httpx.Response, attempt: AttemptCollector, trace: TraceCollector
    ) -> None:
        from litellm.proxy.retail.model_observations import OutputDetector

        self._stream = stream
        self._response = response
        self._attempt = attempt
        self._trace = trace
        self._detector = OutputDetector()
        self._observable = response.headers.get("content-encoding", "identity").lower() in {"", "identity"}
        self._error_body = b""
        self._error_body_limited = False
        self._bytes = 0
        self._chunks = 0
        self._observed_event = False
        self._kinds: frozenset[str] = frozenset()
        self._request_id = _header_id(response.headers)
        self._terminal_close = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self._attempt.event("response_body.started", {})
        try:
            async for chunk in self._stream:
                self._bytes += len(chunk)
                self._chunks += 1
                if self._chunks == 1:
                    self._attempt.event("response_body.first_chunk", {"bytes": len(chunk)})
                self._observe(chunk)
                yield chunk
        except BaseException as exc:
            if not isinstance(exc, GeneratorExit) or not self._close_after_terminal():
                self._attempt.event("response_body.failed", {"error_type": type(exc).__name__, "bytes": self._bytes})
                _finish(self._attempt, self._response.status_code, type(exc).__name__, self._request_id)
            raise
        else:
            self._attempt.event("response_body.complete", {"bytes": self._bytes, "chunks": self._chunks})
            identifier: Final = (
                self._request_id or _body_id(self._error_body)
                if self._error_body and not self._error_body_limited
                else self._request_id
            )
            _finish(
                self._attempt,
                self._response.status_code,
                "HTTPStatusError" if self._response.is_error else None,
                identifier,
            )

    def _observe(self, chunk: bytes) -> None:
        if not self._observable:
            return
        if self._response.is_error and not self._error_body_limited:
            if len(self._error_body) + len(chunk) <= _MAX_ERROR_BYTES:
                self._error_body += chunk
            else:
                self._error_body_limited = True
                self._error_body = b""
        signals: Final = self._detector.observe_signals(chunk)
        if self._detector.has_event and not self._observed_event:
            self._observed_event = True
            self._trace.milestone("upstream_first_event")
            self._attempt.event("upstream_first_event", {})
        for signal in signals:
            if signal.kind in self._kinds:
                continue
            self._kinds = self._kinds.union((signal.kind,))
            self._trace.milestone("first_model_output")
            milestone: Final = {
                "text": "first_text_output",
                "reasoning": "first_reasoning_output",
                "tool_arguments": "first_tool_output",
            }[signal.kind]
            self._trace.milestone(milestone)
            self._attempt.event(milestone, {"protocol": signal.protocol, "kind": signal.kind})

    def observe_buffered(self, body: bytes) -> None:
        self._bytes = len(body)
        self._attempt.event("response_body.buffered", {"bytes": len(body)})
        self._observe(body)
        identifier: Final = self._request_id or (_body_id(self._error_body) if not self._error_body_limited else None)
        _finish(
            self._attempt,
            self._response.status_code,
            "HTTPStatusError" if self._response.is_error else None,
            identifier,
        )

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        except BaseException as exc:
            self._attempt.event("response_body.failed", {"error_type": type(exc).__name__, "bytes": self._bytes})
            _finish(self._attempt, self._response.status_code, type(exc).__name__, self._request_id)
            raise
        finally:
            if self._attempt.snapshot().get("ended_ms") is None:
                if not self._close_after_terminal():
                    self._attempt.event("response_body.closed", {"bytes": self._bytes, "chunks": self._chunks})
                    _finish(self._attempt, self._response.status_code, "StreamClosedBeforeCompletion", self._request_id)

    def _close_after_terminal(self) -> bool:
        terminal: Final = self._detector.terminal_event
        if terminal is None:
            return False
        if not self._terminal_close:
            self._terminal_close = True
            self._attempt.event(
                "response_body.consumer_closed_after_terminal",
                {"terminal_event": terminal, "bytes": self._bytes, "chunks": self._chunks},
            )
            _finish(
                self._attempt,
                self._response.status_code,
                "HTTPStatusError" if self._response.is_error else None,
                self._request_id,
            )
        return True


async def trace_response(response: httpx.Response) -> None:
    trace: Final = _trace()
    if trace is None:
        return
    from litellm.proxy.retail.request_tracing import AttemptCollector

    attempt: Final = response.request.extensions.get(_ATTEMPT_EXTENSION)
    if not isinstance(attempt, AttemptCollector):
        return
    attempt.event(
        "response_headers.complete", {"status_code": response.status_code, "http_version": response.http_version}
    )
    attempt.set_attributes(
        {
            "http_version": response.http_version,
            "content_encoding": response.headers.get("content-encoding", "identity"),
        }
    )
    stream: Final = _TracedResponseStream(response.stream, response, attempt, trace)
    if response.is_stream_consumed:
        stream.observe_buffered(response.content)
    else:
        response.stream = stream  # rebind-ok: HTTPX response hooks wrap the original byte stream without consuming it


def merge_trace_hooks(
    hooks: Mapping[str, Sequence[Callable[..., object]]] | None = None,
) -> dict[str, list[Callable[..., object]]]:  # mutable-ok: HTTPX event_hooks boundary needs mutable hook lists
    previous: Final = hooks or {}
    return {
        **{key: list(value) for key, value in previous.items()},
        "request": [*previous.get("request", ()), trace_request],
        "response": [trace_response, *previous.get("response", ())],
    }
