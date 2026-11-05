import asyncio
import hmac
import io
import json
import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import JsonValue
from starlette.types import Receive, Scope, Send

from .coordination_guard import request_path
from .scim_middleware import SCIM_BASE, SCIM_SCOPE_KEY, ScimCredential
from .scim_protocol import (
    ScimFailure,
    ScimQuery,
    ScimRecord,
    etag,
    parse_create,
    parse_query,
    parse_search,
    render_error,
    render_list,
    render_user,
    resource_types,
    schema_documents,
    service_provider_config,
)
from .scim_service import ScimService, get_scim_service

_PRIVATE_IO: Final = ContextVar("retail_scim_private_io", default=False)
_MAX_BODY: Final = 65536


class _PrivateLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_IO.get()


_LOG_FILTER: Final = _PrivateLogFilter()


def install_scim_private_filter() -> None:
    names: Final = ("", "httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2", "prisma")
    loggers: Final = tuple(logging.getLogger(name) for name in names)
    for logger in loggers:
        if _LOG_FILTER not in logger.filters:
            logger.addFilter(_LOG_FILTER)
    for logger in loggers:
        for handler in logger.handlers:
            if _LOG_FILTER not in handler.filters:
                handler.addFilter(_LOG_FILTER)


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    body: JsonValue | None
    headers: tuple[tuple[bytes, bytes], ...] = ()


def _failed(failure: ScimFailure) -> _Reply:
    headers: Final = ((b"www-authenticate", b'Bearer error="invalid_token"'),) if failure.status == 401 else ()
    return _Reply(failure.status, render_error(failure), headers)


def _header(scope: Scope, name: bytes) -> str | ScimFailure | None:
    values: Final = tuple(value for key, value in scope.get("headers", ()) if key.lower() == name)
    if len(values) > 1:
        return ScimFailure(400, "invalidValue")
    if not values:
        return None
    try:
        return values[0].decode("ascii")
    except UnicodeError:
        return ScimFailure(400, "invalidValue")


def _correct_origin(scope: Scope, service: ScimService) -> bool:
    if scope.get("root_path", "") not in ("", "/"):
        return False
    host: Final = _header(scope, b"host")
    if not isinstance(host, str) or any(character in host for character in "/\\?#"):
        return False
    try:
        actual: Final = urlsplit(f"{scope.get('scheme', '')}://{host}")
        expected: Final = urlsplit(service.setup.config.public_origin)
        actual_port: Final = actual.port if actual.port is not None else 443 if actual.scheme == "https" else 80
        expected_port: Final = expected.port if expected.port is not None else 443 if expected.scheme == "https" else 80
    except ValueError:
        return False
    return (
        actual.username is None and actual.password is None
        and (actual.scheme, actual.hostname, actual_port) == (expected.scheme, expected.hostname, expected_port)
    )


async def _body(receive: Receive) -> bytes | ScimFailure:
    with io.BytesIO() as stream:
        while True:
            message: Final = await receive()
            if message["type"] != "http.request":
                return ScimFailure(400, "invalidValue")
            chunk: Final = message.get("body", b"")
            if stream.tell() + len(chunk) > _MAX_BODY:
                return ScimFailure(413)
            stream.write(chunk)
            if not message.get("more_body", False):
                return stream.getvalue()


def _identifier(path: str) -> UUID | ScimFailure:
    try:
        value: Final = UUID(path)
    except ValueError:
        return ScimFailure(404)
    return value if str(value) == path else ScimFailure(404)


def _single_discovery(payload: JsonValue, identifier: str) -> JsonValue | ScimFailure:
    if not isinstance(payload, dict) or not isinstance(payload.get("Resources"), list):
        return ScimFailure(503)
    return next((row for row in payload["Resources"] if isinstance(row, dict) and row.get("id") == identifier), ScimFailure(404))


def _discovery(path: str, service: ScimService) -> JsonValue | ScimFailure | None:
    if path == "/ServiceProviderConfig":
        return service_provider_config(service.setup.config)
    if path == "/ResourceTypes":
        return resource_types(service.setup.config)
    if path.startswith("/ResourceTypes/"):
        return _single_discovery(resource_types(service.setup.config), path[len("/ResourceTypes/"):])
    if path == "/Schemas":
        return schema_documents(service.setup.config)
    if path.startswith("/Schemas/"):
        return _single_discovery(schema_documents(service.setup.config), path[len("/Schemas/"):])
    return None


def _resource_reply(record: ScimRecord, service: ScimService, status: int = 200) -> _Reply:
    location: Final = f"{service.setup.config.public_origin}{SCIM_BASE}/Users/{record.id}"
    return _Reply(status, render_user(record, service.setup.config), (
        (b"etag", etag(record).encode()), (b"location", location.encode("ascii")),
    ))


async def _collection(service: ScimService, scope: Scope, path: str, body: bytes) -> _Reply | None:
    method: Final = scope.get("method", "")
    if path == "/Users" and method == "GET":
        try:
            params: Final = tuple(parse_qsl(scope.get("query_string", b"").decode("ascii"), keep_blank_values=True, max_num_fields=16))
        except (UnicodeError, ValueError):
            return _failed(ScimFailure(400, "invalidValue"))
        query: Final = parse_query(params)
        if isinstance(query, ScimFailure):
            return _failed(query)
        listed: Final = await service.store.list_users(service.setup, query)
        return _failed(listed) if isinstance(listed, ScimFailure) else _Reply(200, render_list(listed, service.setup.config))
    if path == "/Users/.search" and method == "POST":
        search_query: Final = parse_search(body)
        if isinstance(search_query, ScimFailure):
            return _failed(search_query)
        searched: Final = await service.store.list_users(service.setup, search_query)
        return _failed(searched) if isinstance(searched, ScimFailure) else _Reply(200, render_list(searched, service.setup.config))
    if path == "/Users" and method == "POST":
        profile: Final = parse_create(body)
        if isinstance(profile, ScimFailure):
            return _failed(profile)
        created: Final = await service.store.create_user(service.setup, profile)
        return _failed(created) if isinstance(created, ScimFailure) else _resource_reply(created, service, 201)
    return None


async def _single_user(service: ScimService, scope: Scope, path: str, body: bytes) -> _Reply:
    if not path.startswith("/Users/"):
        return _failed(ScimFailure(404))
    identifier: Final = _identifier(path[len("/Users/"):])
    if isinstance(identifier, ScimFailure):
        return _failed(identifier)
    method: Final = scope.get("method", "")
    if_match: Final = _header(scope, b"if-match")
    if isinstance(if_match, ScimFailure):
        return _failed(if_match)
    if method == "GET":
        found: Final = await service.store.get_user(service.setup, identifier)
        return _failed(found) if isinstance(found, ScimFailure) else _resource_reply(found, service)
    if method in {"PATCH", "PUT"}:
        updated: Final = await service.store.update_user(
            service.setup, identifier, body, replace=method == "PUT", if_match=if_match,
        )
        return _failed(updated) if isinstance(updated, ScimFailure) else _resource_reply(updated, service)
    if method == "DELETE":
        removed: Final = await service.store.delete_user(service.setup, identifier, if_match)
        return _failed(removed) if isinstance(removed, ScimFailure) else _Reply(204, None)
    return _failed(ScimFailure(405))


async def _dispatch(service: ScimService, scope: Scope, body: bytes) -> _Reply:
    path: Final = request_path(scope)[len(SCIM_BASE):]
    method: Final = scope.get("method", "")
    if (path != "/Users" or method != "GET") and scope.get("query_string", b""):
        return _failed(ScimFailure(400, "invalidValue"))
    if method == "GET":
        discovery: Final = _discovery(path, service)
        if isinstance(discovery, ScimFailure):
            return _failed(discovery)
        if discovery is not None:
            checked: Final = await service.store.list_users(service.setup, ScimQuery(count=0))
            return _failed(checked) if isinstance(checked, ScimFailure) else _Reply(200, discovery)
    collection: Final = await _collection(service, scope, path, body)
    return collection if collection is not None else await _single_user(service, scope, path, body)


class ScimHttpApplication:
    def __init__(self, *, get_service: Callable[[], ScimService | ScimFailure] = get_scim_service) -> None:
        self._get_service = get_service
        self._slots = asyncio.Semaphore(8)
        install_scim_private_filter()

    async def _authorized(self, scope: Scope, receive: Receive) -> _Reply:
        credential: Final = scope.get(SCIM_SCOPE_KEY)
        if not isinstance(credential, ScimCredential):
            return _failed(ScimFailure(401))
        service: Final = self._get_service()
        if isinstance(service, ScimFailure):
            return _failed(service)
        if not hmac.compare_digest(credential.token.get_secret_value(), service.setup.token.get_secret_value()):
            return _failed(ScimFailure(401))
        if not _correct_origin(scope, service):
            return _failed(ScimFailure(400, "invalidValue"))
        method: Final = scope.get("method", "")
        if method in {"POST", "PATCH", "PUT"}:
            media_type: Final = _header(scope, b"content-type")
            length: Final = _header(scope, b"content-length")
            if isinstance(media_type, ScimFailure) or isinstance(length, ScimFailure):
                return _failed(ScimFailure(400, "invalidValue"))
            if not isinstance(media_type, str) or media_type.split(";", 1)[0].strip().lower() not in {"application/json", "application/scim+json"}:
                return _failed(ScimFailure(415))
            if isinstance(length, str) and (not length.isdigit() or len(length) > 8):
                return _failed(ScimFailure(400, "invalidValue"))
            if isinstance(length, str) and int(length) > _MAX_BODY:
                return _failed(ScimFailure(413))
            try:
                async with asyncio.timeout(5):
                    body: Final = await _body(receive)
            except TimeoutError:
                return _failed(ScimFailure(400, "invalidValue"))
            if isinstance(body, ScimFailure):
                return _failed(body)
            return await _dispatch(service, scope, body)
        return await _dispatch(service, scope, b"")

    async def _limited(self, scope: Scope, receive: Receive) -> _Reply:
        try:
            async with asyncio.timeout(2):
                await self._slots.acquire()
        except TimeoutError:
            return _failed(ScimFailure(429))
        try:
            return await self._authorized(scope, receive)
        finally:
            self._slots.release()

    async def _safe_reply(self, scope: Scope, receive: Receive) -> _Reply:
        try:
            return await self._limited(scope, receive)
        except Exception:  # noqa: BLE001  # directory profiles and credentials must never escape into native exception logs
            return _failed(ScimFailure(503))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        private: Final = _PRIVATE_IO.set(True)
        try:
            reply: Final = await self._safe_reply(scope, receive)
        finally:
            _PRIVATE_IO.reset(private)
        payload: Final = b"" if reply.body is None else json.dumps(reply.body, ensure_ascii=False, separators=(",", ":")).encode()
        await send({"type": "http.response.start", "status": reply.status, "headers": [
            (b"content-type", b"application/scim+json"), (b"cache-control", b"private, no-store"),
            (b"referrer-policy", b"no-referrer"), (b"x-content-type-options", b"nosniff"),
            (b"content-length", str(len(payload)).encode()), *reply.headers,
        ]})
        await send({"type": "http.response.body", "body": payload})
