import json
import logging
import zlib
from collections.abc import Mapping, Sequence
from typing import Final, Protocol
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from litellm.proxy._types import LiteLLMRoutes
from litellm.proxy.auth.resolvers.models import Principal
from litellm.proxy.auth.roles import Role

from .customer_response_privacy import (
    CustomerSSEPrivacy,
    customer_error_body,
    customer_response_headers,
    customer_response_payload,
)
from .request_tracing import current_trace
from .trace_middleware import traced_path

_JSON_LIMIT: Final = 64 * 1024 * 1024
_ERROR_LIMIT: Final = 64 * 1024
_STREAM_CHUNK_LIMIT: Final = 16 * 1024 * 1024
_INFERENCE_ROUTES: Final = frozenset(LiteLLMRoutes.llm_api_routes.value)
_LOG: Final = logging.getLogger(__name__)


def _admin_scope(scope: Scope) -> bool:
    state: Final = scope.get("state")
    principal: Final = state.get("principal") if isinstance(state, Mapping) else None
    return isinstance(principal, Principal) and any(
        role in {Role.PLATFORM_ADMIN, Role.PLATFORM_VIEWER} for role in principal.roles
    )


def _inference_scope(scope: Scope) -> bool:
    template: Final = getattr(scope.get("route"), "path", scope["path"])
    path: Final = scope["path"]
    return template in _INFERENCE_ROUTES or traced_path(path) or path.startswith((
        "/openai/", "/anthropic/", "/gemini/", "/vertex_ai/",
    ))


def _header(headers: Sequence[tuple[bytes, bytes]], name: bytes) -> str:
    return next((value.decode("latin-1").lower() for key, value in headers if key.lower() == name), "")


class _Decoder(Protocol):
    @property
    def eof(self) -> bool: ...

    @property
    def unconsumed_tail(self) -> bytes: ...

    def decompress(self, data: bytes, max_length: int = 0) -> bytes: ...


def _decoder(encoding: str) -> _Decoder | None:
    if encoding in {"gzip", "x-gzip"}:
        return zlib.decompressobj(zlib.MAX_WBITS | 16)
    if encoding == "deflate":
        return zlib.decompressobj()
    return None


class _CustomerResponse:
    def __init__(self, scope: Scope, send: Send) -> None:
        self._scope = scope
        self._send = send
        self._active = False
        self._status = 200
        self._start: Message | None = None
        self._json = False
        self._stream: CustomerSSEPrivacy | None = None
        self._body = bytearray()
        self._omitted = False
        self._encoding = ""
        self._decoder: _Decoder | None = None
        self._stream_failed = False
        self._request_id: str | None = None
        self._body_limit = _JSON_LIMIT

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            await self._response_start(message)
            return
        if message["type"] != "http.response.body" or not self._active:
            await self._send(message)
            return
        if self._json:
            await self._json_body(message)
            return
        if self._stream is not None:
            await self._stream_body(message)
            return
        await self._send(message)

    async def _response_start(self, message: Message) -> None:
        self._status = message["status"]
        private_cache: Final = self._scope["path"].startswith((
            "/spend/logs", "/v2/spend/logs", "/v3/spend/logs", "/retail/admin/request-traces/",
        ))
        start: Final = {**message, "headers": [
            (key, value) for key, value in message.get("headers", []) if key.lower() != b"cache-control"
        ] + [(b"cache-control", b"no-store")]} if private_cache else message
        inference: Final = _inference_scope(self._scope)
        self._active = not _admin_scope(self._scope) and (inference or self._status >= 400)
        if not self._active:
            await self._send(start)
            return
        trace: Final = current_trace()
        self._request_id = (
            (trace.call_id or trace.trace_id) if trace is not None else str(uuid4()) if inference else None
        )
        if trace is None and inference:
            _LOG.info("Public inference response id=%s status=%s", self._request_id, self._status)
        raw_headers: Final = start.get("headers", [])
        content_type: Final = _header(raw_headers, b"content-type")
        self._encoding = _header(raw_headers, b"content-encoding")
        headers: Final = customer_response_headers(raw_headers, request_id=self._request_id)
        self._json = self._status >= 400 or "json" in content_type
        self._body_limit = _ERROR_LIMIT if self._status >= 400 else _JSON_LIMIT
        if "text/event-stream" in content_type:
            self._json = False
            self._stream = CustomerSSEPrivacy(status_code=self._status, request_id=self._request_id)
            self._decoder = _decoder(self._encoding)
        rewritten: Final = self._json or self._stream is not None
        self._start = {**start, "headers": [
            (key, value) for key, value in headers
            if not rewritten or key.lower() not in {
                b"content-length", b"content-encoding", b"content-type", b"etag", b"last-modified",
            }
        ] + ([(b"content-type", b"application/json")] if self._json else
             [(b"content-type", b"text/event-stream")] if self._stream is not None else [])}
        if self._json:
            return
        await self._send(self._start)

    def _decoded(self, body: bytes, limit: int, *, final: bool = False) -> bytes | None:
        if self._encoding in {"", "identity"}:
            return body if len(body) <= limit else None
        decoder: Final = self._decoder or _decoder(self._encoding)
        if decoder is None:
            return None
        try:
            decoded: Final = decoder.decompress(body, limit + 1)
        except zlib.error:
            return None
        return decoded if (
            len(decoded) <= limit and not decoder.unconsumed_tail and (not final or decoder.eof)
        ) else None

    def _public_json(self, decoded: bytes | None) -> tuple[bytes, bool]:
        fallback_status: Final = self._status if self._status >= 400 else 502
        if decoded is None:
            return customer_error_body(status_code=fallback_status, request_id=self._request_id), True
        try:
            payload: Final[object] = json.loads(decoded)
            safe: Final = customer_response_payload(
                payload, status_code=self._status, request_id=self._request_id,
            )
            if safe is not None:
                return json.dumps(safe, ensure_ascii=False).encode("utf-8"), False
            if self._status < 400:
                return decoded, False
        except (ValueError, UnicodeError):
            return customer_error_body(status_code=fallback_status, request_id=self._request_id), True
        return customer_error_body(status_code=fallback_status, request_id=self._request_id), True

    async def _json_body(self, message: Message) -> None:
        body: Final = message.get("body", b"")
        direct: Final = (
            not message.get("more_body", False) and not self._body and not self._omitted and len(body) <= self._body_limit
        )
        if not direct:
            if not self._omitted and len(self._body) + len(body) <= self._body_limit:
                self._body.extend(body)
            else:
                self._omitted = True
                self._body.clear()
        if message.get("more_body", False):
            return
        decoded: Final = None if self._omitted else self._decoded(
            body if direct else bytes(self._body), self._body_limit, final=True,
        )
        output, failed = self._public_json(decoded)
        if self._start is not None:
            await self._send({**self._start, "status": 502} if failed and self._status < 400 else self._start)
        await self._send({**message, "body": output, "more_body": False})
        self._body.clear()

    async def _stream_body(self, message: Message) -> None:
        if self._stream is None:
            return
        if self._stream_failed:
            await self._send({**message, "body": b""})
            return
        final: Final = not message.get("more_body", False)
        decoded: Final = self._decoded(message.get("body", b""), _STREAM_CHUNK_LIMIT, final=final)
        if decoded is None:
            self._stream_failed = True
            safe_error: Final = customer_error_body(status_code=502, request_id=self._request_id)
            await self._send({**message, "body": b"event: error\ndata: " + safe_error + b"\n\n"})
            return
        await self._send({**message, "body": self._stream.feed(decoded, final=final)})


class CustomerResponsePrivacyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(("/ui/", "/static/")):
            await self.app(scope, receive, send)
            return
        response: Final = _CustomerResponse(scope, send)
        await self.app(scope, receive, response.send)
