import os
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_JSON_ENDPOINTS: Final = frozenset(
    (
        "/chat/completions",
        "/completions",
        "/messages",
        "/messages/count_tokens",
        "/responses",
        "/embeddings",
        "/images/generations",
        "/audio/speech",
        "/rerank",
        "/search",
        "/moderations",
    )
)
_PROVIDER_PREFIXES: Final = ("/openai/", "/openai_passthrough/", "/anthropic/", "/gemini/", "/vertex_ai/")
_JSON_METHODS: Final = (
    ":generateContent",
    ":streamGenerateContent",
    ":countTokens",
    ":predict",
    ":rawPredict",
    ":streamRawPredict",
)
_ERRORS: Final = MappingProxyType(
    {
        400: b'{"error":{"message":"Invalid Content-Length header","type":"invalid_request_error","code":"400"}}',
        413: (
            b'{"error":{"message":"Request body exceeds the configured byte limit",'
            b'"type":"invalid_request_error","code":"413"}}'
        ),
        415: (
            b'{"error":{"message":"Encoded JSON request bodies are unsupported",'
            b'"type":"invalid_request_error","code":"415"}}'
        ),
    }
)


@dataclass(frozen=True, slots=True)
class _HeaderFailure:
    status: int


class _BodyLimitExceeded(Exception):
    pass


def request_body_limit_setting() -> int:
    value: Final = os.environ.get("RETAIL_MAX_REQUEST_BYTES", "0")
    try:
        limit: Final = int(value)
    except ValueError as exc:
        raise ValueError("RETAIL_MAX_REQUEST_BYTES must be an integer between 0 and 1073741824") from exc
    if not 0 <= limit <= 1024 * 1024 * 1024:
        raise ValueError("RETAIL_MAX_REQUEST_BYTES must be an integer between 0 and 1073741824")
    return limit


def _declared_length(headers: Sequence[tuple[bytes, bytes]]) -> int | _HeaderFailure | None:
    values: Final = tuple(value.strip() for key, value in headers if key.lower() == b"content-length")
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit() or len(values[0]) > 20:
        return _HeaderFailure(400)
    return int(values[0])


def _json_inference(scope: Scope) -> bool:
    incoming: Final = scope["path"]
    root_path: Final = scope.get("root_path", "").rstrip("/")
    path: Final = (
        incoming.removeprefix(root_path) if root_path and incoming.startswith(root_path + "/") else incoming
    ).rstrip("/")
    segments: Final = path.split("/", 2)
    normalized: Final = (
        "/" + segments[2] if len(segments) == 3 and segments[1].startswith("v") and segments[1][1:].isdigit() else path
    )
    direct: Final = normalized in _JSON_ENDPOINTS or path.endswith(_JSON_METHODS)
    if not direct and not path.startswith(_PROVIDER_PREFIXES):
        return False
    content_type: Final = next(
        (value.split(b";", 1)[0].strip().lower() for key, value in scope["headers"] if key.lower() == b"content-type"),
        b"",
    )
    return content_type == b"application/json" or content_type.endswith(b"+json") or (not content_type and direct)


async def _reject(send: Send, status: int) -> None:
    body: Final = _ERRORS[status]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class _BodyGuard:
    def __init__(self, receive: Receive, send: Send, limit: int) -> None:
        self._receive = receive
        self._send = send
        self._limit = limit
        self._received = 0
        self.exceeded = False
        self._started = False
        self.rejected = False

    async def receive(self) -> Message:
        if self.exceeded:
            raise _BodyLimitExceeded
        message: Final = await self._receive()
        if message["type"] == "http.request":
            self._received += len(message.get("body", b""))
            if self._received > self._limit:
                self.exceeded = True
                raise _BodyLimitExceeded
        return message

    async def reject(self) -> None:
        if self.rejected:
            return
        if self._started:
            raise _BodyLimitExceeded
        self._started = True
        self.rejected = True
        await _reject(self._send, 413)

    async def send(self, message: Message) -> None:
        if self.exceeded:
            await self.reject()
            return
        if message["type"] == "http.response.start":
            self._started = True
        await self._send(message)


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_request_bytes: int | None = None) -> None:
        limit: Final = request_body_limit_setting() if max_request_bytes is None else max_request_bytes
        if isinstance(limit, bool) or not 0 <= limit <= 1024 * 1024 * 1024:
            raise ValueError("The request byte limit must be between 0 and 1073741824")
        self.app = app
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._limit == 0:
            await self.app(scope, receive, send)
            return
        length: Final = _declared_length(scope["headers"])
        if isinstance(length, _HeaderFailure):
            await _reject(send, length.status)
            return
        if length is not None and length > self._limit:
            await _reject(send, 413)
            return
        if _json_inference(scope) and any(
            value.strip().lower() not in (b"", b"identity")
            for key, value in scope["headers"]
            if key.lower() == b"content-encoding"
        ):
            await _reject(send, 415)
            return
        guard: Final = _BodyGuard(receive, send, self._limit)
        try:
            await self.app(scope, guard.receive, guard.send)
        except Exception:  # noqa: BLE001  # native routes may translate a size overflow into another exception
            if not guard.exceeded:
                raise
            await guard.reject()
            return
        if guard.exceeded:
            await guard.reject()
