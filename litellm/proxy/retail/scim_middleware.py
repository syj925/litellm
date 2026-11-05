import logging
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import parse_qsl, unquote_to_bytes

from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_jwt_middleware import _credential_names
from .coordination_guard import request_path

SCIM_SCOPE_KEY: Final = "retail_scim_credential"
SCIM_BASE: Final = "/retail/scim/v2"
SCIM_TOKEN_PREFIX: Final = b"rt-scim."
_REJECTED: Final = b'{"schemas":["urn:ietf:params:scim:api:messages:2.0:Error"],"status":"401","detail":"Directory authentication failed"}'
_TOKEN_FIELDS: Final = frozenset({"key", "api_key", "token", "access_token", "authorization"})


@dataclass(frozen=True, slots=True)
class ScimCredential:
    token: SecretStr = field(repr=False)


def scim_path(path: str) -> bool:
    return path == SCIM_BASE or path.startswith(SCIM_BASE + "/")


def _reserved(value: bytes) -> bool:
    return SCIM_TOKEN_PREFIX in unquote_to_bytes(value.replace(b"+", b" ")).lower()


def _query_credentials(query: bytes) -> bool:
    try:
        fields: Final = tuple(parse_qsl(query.decode("ascii"), keep_blank_values=True, max_num_fields=16))
    except (UnicodeError, ValueError):
        return True
    return any(name.lower() in _TOKEN_FIELDS for name, _ in fields)


async def _reject(scope: Scope, send: Send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008, "reason": "Directory authentication failed"})
        return
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"content-type", b"application/scim+json"), (b"cache-control", b"private, no-store"),
        (b"content-length", str(len(_REJECTED)).encode()), (b"referrer-policy", b"no-referrer"),
        (b"www-authenticate", b'Bearer error="invalid_token"'), (b"x-content-type-options", b"nosniff"),
    ]})
    await send({"type": "http.response.body", "body": _REJECTED})


class ScimIngressMiddleware:
    def __init__(self, app: ASGIApp, *, directory_app: ASGIApp | None = None) -> None:
        from .scim_http import ScimHttpApplication

        self.app = app
        self.directory_app = directory_app if directory_app is not None else ScimHttpApplication()
        install_scim_access_filter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path: Final = request_path(scope)
        directory: Final = scim_path(path)
        headers: Final = tuple(scope.get("headers", ()))
        names: Final = _credential_names()
        credentials: Final = tuple((name.lower(), value) for name, value in headers if name.lower() in names)
        reserved_credential: Final = any(_reserved(value) for _, value in credentials)
        reserved_cookie: Final = any(name.lower() == b"cookie" and _reserved(value) for name, value in headers)
        query: Final = scope.get("query_string", b"")
        reserved_uri: Final = _reserved(query) or _reserved(path.encode())
        if not directory and not reserved_credential and not reserved_cookie and not reserved_uri:
            await self.app(scope, receive, send)
            return
        if (
            not directory or scope["type"] != "http" or len(credentials) != 1 or credentials[0][0] != b"authorization"
            or reserved_cookie or len(query) > 8192 or reserved_uri or _query_credentials(query)
            or any(name.lower() == b"cookie" for name, _ in headers)
            or any(name.lower() == b"x-litellm-disable-callbacks" for name, _ in headers)
        ):
            await _reject(scope, send)
            return
        authorization: Final = credentials[0][1]
        token: Final = authorization[7:] if authorization[:7].lower() == b"bearer " else b""
        if (
            not token.startswith(SCIM_TOKEN_PREFIX) or not 40 <= len(token) <= 264
            or any(character not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_." for character in token)
        ):
            await _reject(scope, send)
            return
        sanitized: Final = {
            **scope, "headers": tuple((name, value) for name, value in headers if name.lower() not in names and not _reserved(value)),
            SCIM_SCOPE_KEY: ScimCredential(SecretStr(token.decode("ascii"))),
        }
        await self.directory_app(sanitized, receive, send)


class ScimAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args: Final = record.args
        if not isinstance(args, tuple) or len(args) != 5 or not isinstance(args[2], str):
            return True
        target: Final = args[2]
        path, _, _query = target.partition("?")
        if len(target) > 32768 or _reserved(path.encode()):
            record.args = (args[0], args[1], "/[credential-redacted]", args[3], args[4])  # rebind-ok: sanitize the shared access record before handlers emit credentials
        elif SCIM_BASE.encode() in unquote_to_bytes(path.encode()):
            record.args = (args[0], args[1], SCIM_BASE + "/[resource]", args[3], args[4])  # rebind-ok: directory paths and filter queries may contain PII
        elif _reserved(target.encode()):
            record.args = (args[0], args[1], path, args[3], args[4])  # rebind-ok: omit credentials from query access logs
        return True


_ACCESS_FILTER: Final = ScimAccessLogFilter()


def install_scim_access_filter() -> None:
    logger: Final = logging.getLogger("uvicorn.access")
    if _ACCESS_FILTER not in logger.filters:
        logger.addFilter(_ACCESS_FILTER)
