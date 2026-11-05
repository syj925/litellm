import logging
import sys
from typing import Final
from urllib.parse import parse_qsl, unquote_to_bytes

from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from litellm.types.proxy.auth.special_headers import SpecialHeaders

from .api_jwt_auth import API_JWT_PREFIX, API_JWT_ROUTES, API_JWT_SCOPE_KEY, ApiJwtInput
from .coordination_guard import request_path

_PREFIX: Final = API_JWT_PREFIX.encode()
_CREDENTIALS: Final = frozenset(name.encode() for name in SpecialHeaders.litellm_credential_header_names()) | {
    b"x-mcp-auth", b"proxy-authorization", b"litellm_user_api_key",
}
_QUERY_CREDENTIALS: Final = frozenset({"key", "api_key", "access_token", "token", "authorization"})
_REJECTED: Final = b'{"error":{"message":"API access token authentication failed","type":"authentication_error","code":"invalid_token"}}'


def _wrapped(value: bytes) -> bytes | None:
    stripped: Final = value.strip()
    token: Final = stripped[7:] if stripped[:7].lower() == b"bearer " else stripped
    return token[len(_PREFIX):] if token.startswith(_PREFIX) else None


def _contains_prefix(value: bytes) -> bool:
    return _PREFIX in unquote_to_bytes(value.replace(b"+", b" ")).lower()


def _token_cookie(headers: tuple[tuple[bytes, bytes], ...]) -> bool:
    for name, value in headers:
        if name.lower() == b"cookie" and any(field.partition(b"=")[0].strip().lower() == b"token" for field in value.split(b";")):
            return True
    return False


def _reserved_token_cookie(headers: tuple[tuple[bytes, bytes], ...]) -> bool:
    return any(
        name.lower() == b"cookie" and any(
            field.partition(b"=")[0].strip().lower() == b"token" and _PREFIX in field.partition(b"=")[2].lower()
            for field in value.split(b";")
        ) for name, value in headers
    )


def _credential_names() -> frozenset[bytes]:
    native: Final = sys.modules.get("litellm.proxy.proxy_server")
    settings: Final = getattr(native, "general_settings", None)
    custom: Final = settings.get("litellm_key_header_name") if isinstance(settings, dict) else None
    return _CREDENTIALS | {custom.lower().encode()} if isinstance(custom, str) else _CREDENTIALS


def _query_credentials(query: bytes) -> bool:
    try:
        fields: Final = tuple(parse_qsl(query.decode("ascii"), keep_blank_values=True, max_num_fields=32))
    except (UnicodeError, ValueError):
        return True
    return any(name.lower() in _QUERY_CREDENTIALS for name, _ in fields)


async def _reject(scope: Scope, send: Send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008, "reason": "API access token authentication failed"})
        return
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(_REJECTED)).encode()),
        (b"cache-control", b"private, no-store"), (b"www-authenticate", b'Bearer error="invalid_token"'),
    ]})
    await send({"type": "http.response.body", "body": _REJECTED})


class ApiJwtCredentialMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        install_api_jwt_access_filter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers: Final = tuple(scope.get("headers", ()))
        names: Final = _credential_names()
        credentials: Final = tuple((name.lower(), value) for name, value in headers if name.lower() in names)
        candidates: Final = tuple((name, value) for name, value in credentials if _PREFIX in value.lower())
        query: Final = scope.get("query_string", b"")
        if not candidates and not _contains_prefix(query) and not _reserved_token_cookie(headers):
            await self.app(scope, receive, send)
            return
        if (
            len(candidates) != 1 or len(credentials) != 1 or candidates[0] != credentials[0]
            or scope["type"] != "http" or scope.get("method") != "POST" or request_path(scope) not in API_JWT_ROUTES
            or _token_cookie(headers) or len(query) > 8192 or _contains_prefix(query) or _query_credentials(query)
            or any(name.lower() == b"x-litellm-disable-callbacks" for name, _ in headers)
        ):
            await _reject(scope, send)
            return
        token: Final = _wrapped(candidates[0][1])
        if token is None or not 1 <= len(token) <= 16384 or any(character < 33 or character > 126 for character in token):
            await _reject(scope, send)
            return
        sanitized: Final = {
            **scope, "headers": tuple((name, value) for name, value in headers if name.lower() not in names and _PREFIX not in value.lower()),
            API_JWT_SCOPE_KEY: ApiJwtInput(SecretStr(token.decode("ascii"))),
        }
        await self.app(sanitized, receive, send)


class ApiJwtAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args: Final = record.args
        if not isinstance(args, tuple) or len(args) != 5 or not isinstance(args[2], str):
            return True
        target: Final = args[2]
        path, _, query = target.partition("?")
        if len(target) > 32768 or _contains_prefix(path.encode()):
            record.args = (args[0], args[1], "/[credential-redacted]", args[3], args[4])  # rebind-ok: sanitize logging's shared record before Python 3.10+ handlers emit credentials
        elif query and (_contains_prefix(query.encode()) or _query_credentials(query.encode())):
            record.args = (args[0], args[1], path, args[3], args[4])  # rebind-ok: redact credential query fields before access-log handlers
        return True


_ACCESS_FILTER: Final = ApiJwtAccessLogFilter()


def install_api_jwt_access_filter() -> None:
    logger: Final = logging.getLogger("uvicorn.access")
    if _ACCESS_FILTER not in logger.filters:
        logger.addFilter(_ACCESS_FILTER)
