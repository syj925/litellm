import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from typing import Final
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from starlette.types import Receive, Scope, Send

from litellm.proxy.retail.scim_config import ScimConfig, ScimSetup
from litellm.proxy.retail.scim_http import ScimHttpApplication
from litellm.proxy.retail.scim_middleware import ScimIngressMiddleware
from litellm.proxy.retail.scim_protocol import (
    USER_SCHEMA,
    ScimFailure,
    ScimList,
    ScimProfile,
    ScimQuery,
    ScimRecord,
    etag,
)
from litellm.proxy.retail.scim_service import ScimService

TOKEN: Final = "rt-scim." + "q" * 48
ORIGIN: Final = "https://directory.example.invalid"
BASE: Final = "/retail/scim/v2"
RECORD: Final = ScimRecord(
    id=UUID(int=1), user_id="retail-scim-user:fixture", namespace="fixture",
    external_id="private-external", user_name="private-directory-name", display_name="Private Directory Name",
    active=True, version=1, credential_revision=UUID(int=2),
    created_at=datetime.fromtimestamp(0, timezone.utc), updated_at=datetime.fromtimestamp(0, timezone.utc),
)


class Authority:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False
        self.initialize_failure: ScimFailure | None = None
        self.configure_failure: ScimFailure | None = None
        self.throw_on_read = False
        self.profile: ScimProfile | None = None
        self.last_match: str | None = None

    async def initialize(self) -> ScimFailure | None:
        return self.initialize_failure

    async def configure(self, setup: ScimSetup) -> ScimFailure | None:
        return self.configure_failure

    async def list_users(self, setup: ScimSetup, query: ScimQuery) -> ScimList | ScimFailure:
        self.calls += 1
        if self.throw_on_read:
            logging.getLogger("prisma").warning("directory query contains %s", RECORD.external_id)
            raise RuntimeError(RECORD.user_name + TOKEN)
        return ScimList(resources=(RECORD,) if query.count else (), total=1, start_index=query.start_index)

    async def get_user(self, setup: ScimSetup, identifier: UUID) -> ScimRecord | ScimFailure:
        self.calls += 1
        return RECORD if identifier == RECORD.id else ScimFailure(404)

    async def create_user(self, setup: ScimSetup, profile: ScimProfile) -> ScimRecord | ScimFailure:
        self.calls += 1
        self.profile = profile
        return RECORD.model_copy(update=profile.model_dump())

    async def update_user(
        self, setup: ScimSetup, identifier: UUID, body: bytes, *, replace: bool, if_match: str | None,
    ) -> ScimRecord | ScimFailure:
        self.calls += 1
        self.last_match = if_match
        return ScimFailure(412) if if_match != etag(RECORD) else RECORD

    async def delete_user(self, setup: ScimSetup, identifier: UUID, if_match: str | None) -> ScimFailure | None:
        self.calls += 1
        self.last_match = if_match
        return None

    async def close(self) -> None:
        self.closed = True


class NativeApp:
    def __init__(self) -> None:
        self.calls = 0
        self.headers: tuple[tuple[bytes, bytes], ...] = ()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        self.headers = tuple(scope.get("headers", ()))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"native":true}'})


def fixture_app(store: Authority, native: NativeApp | None = None) -> ScimIngressMiddleware:
    setup: Final = ScimSetup(ScimConfig(namespace="fixture", generation=1, public_origin=ORIGIN), SecretStr(TOKEN))
    service: Final = ScimService(setup, store)
    service.ready = True
    return ScimIngressMiddleware(native or NativeApp(), directory_app=ScimHttpApplication(get_service=lambda: service))


@pytest.mark.asyncio
async def test_directory_create_projects_only_scim_attributes_and_never_enters_native_app() -> None:
    store: Final = Authority()
    native: Final = NativeApp()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture_app(store, native)), base_url=ORIGIN) as client:
        response: Final = await client.post(BASE + "/Users", headers={"Authorization": "Bearer " + TOKEN}, json={
            "schemas": [USER_SCHEMA], "externalId": RECORD.external_id, "userName": RECORD.user_name, "active": True,
        })
    assert response.status_code == 201, response.text
    assert store.profile == ScimProfile(external_id=RECORD.external_id, user_name=RECORD.user_name, active=True)
    assert native.calls == 0 and store.calls == 1
    assert RECORD.user_id not in response.text and TOKEN not in response.text
    assert response.headers["Location"] == ORIGIN + BASE + "/Users/" + str(RECORD.id)
    assert response.headers["ETag"] == etag(RECORD)
    assert response.json()["schemas"] == [USER_SCHEMA]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ("/ServiceProviderConfig", "/ResourceTypes", "/ResourceTypes/User", "/Schemas", "/Schemas/" + USER_SCHEMA, "/Users", "/Users/" + str(RECORD.id)))
async def test_authenticated_discovery_and_reads_are_private_and_fenced(path: str) -> None:
    store: Final = Authority()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture_app(store)), base_url=ORIGIN) as client:
        response: Final = await client.get(BASE + path, headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 200, response.text
    assert store.calls == 1
    assert "no-store" in response.headers["Cache-Control"] and response.headers["Referrer-Policy"] == "no-referrer"
    assert TOKEN not in response.text and RECORD.user_id not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", (
    {}, {"Authorization": "Bearer sk-fixture-master"}, {"Authorization": "Bearer rt-scim." + "z" * 48},
    {"Authorization": "Bearer " + TOKEN, "x-api-key": "sk-other"},
    {"Authorization": "Bearer " + TOKEN, "Cookie": "token=another-session"},
    {"Authorization": "Bearer " + TOKEN, "x-litellm-disable-callbacks": "all"},
))
async def test_wrong_or_ambiguous_directory_credentials_never_reach_storage(headers: Mapping[str, str]) -> None:
    store: Final = Authority()
    native: Final = NativeApp()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture_app(store, native)), base_url=ORIGIN) as client:
        response: Final = await client.get(BASE + "/Users", headers=headers)
    assert response.status_code == 401
    assert response.json()["status"] == "401" and store.calls == 0 and native.calls == 0
    assert TOKEN not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ("/v1/chat/completions", "/retail/wallet", "/retail/scim/v2/Users?access_token=private", "/v1/models?key=" + TOKEN))
async def test_reserved_directory_credential_never_falls_back_to_native_auth(path: str) -> None:
    native: Final = NativeApp()
    store: Final = Authority()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture_app(store, native)), base_url=ORIGIN) as client:
        response: Final = await client.get(path, headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 401 and native.calls == 0 and store.calls == 0
    assert TOKEN not in response.text


@pytest.mark.asyncio
async def test_ordinary_native_headers_are_unchanged_by_directory_ingress() -> None:
    native: Final = NativeApp()
    store: Final = Authority()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture_app(store, native)), base_url=ORIGIN) as client:
        response: Final = await client.get("/retail/wallet", headers={"Authorization": "Bearer sk-ordinary", "User-Agent": "docs mention rt-scim.example"})
    assert response.json() == {"native": True}
    assert (b"authorization", b"Bearer sk-ordinary") in native.headers and store.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type,body,status", (
    ("text/plain", b"{}", 415), ("application/json", b"x" * 65537, 413),
    ("application/json", b'{"schemas":[],"password":"private"}', 400),
))
async def test_invalid_or_oversize_body_rejects_before_native_storage(content_type: str, body: bytes, status: int) -> None:
    store: Final = Authority()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture_app(store)), base_url=ORIGIN) as client:
        response: Final = await client.post(BASE + "/Users", content=body, headers={"Authorization": "Bearer " + TOKEN, "Content-Type": content_type})
    assert response.status_code == status and store.calls == 0
    assert "private" not in response.json()["detail"]


@pytest.mark.asyncio
async def test_chunked_body_limit_is_enforced_without_a_content_length() -> None:
    store: Final = Authority()
    third_chunk_consumed: Final = asyncio.Event()

    async def content() -> AsyncIterator[bytes]:
        yield b"x" * 32768
        yield b"y" * 32769
        third_chunk_consumed.set()
        yield b"must-not-be-read"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture_app(store)), base_url=ORIGIN) as client:
        response: Final = await client.post(BASE + "/Users", content=content(), headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/scim+json"})
    assert response.status_code == 413 and store.calls == 0
    assert not third_chunk_consumed.is_set()


@pytest.mark.asyncio
async def test_directory_storage_exception_cannot_expose_credentials_or_profiles(caplog: pytest.LogCaptureFixture) -> None:
    store: Final = Authority()
    store.throw_on_read = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture_app(store)), base_url=ORIGIN) as client:
        response: Final = await client.get(BASE + "/Users", headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 503
    assert TOKEN not in response.text + caplog.text
    assert RECORD.external_id not in response.text + caplog.text and RECORD.user_name not in response.text + caplog.text


@pytest.mark.asyncio
async def test_resource_versions_and_empty_delete_response_preserve_http_contract() -> None:
    store: Final = Authority()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture_app(store)), base_url=ORIGIN) as client:
        response: Final = await client.patch(BASE + "/Users/" + str(RECORD.id), json={}, headers={"Authorization": "Bearer " + TOKEN, "If-Match": 'W/"stale"'})
        deleted: Final = await client.delete(BASE + "/Users/" + str(RECORD.id), headers={"Authorization": "Bearer " + TOKEN, "If-Match": etag(RECORD)})
    assert response.status_code == 412 and response.json()["status"] == "412"
    assert deleted.status_code == 204 and deleted.content == b"" and store.last_match == etag(RECORD)
