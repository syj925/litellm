from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import httpx
import pytest
from fastapi import FastAPI

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail.api import retail_admin
from litellm.proxy.retail.request_policies import router
from litellm.proxy.retail.request_policy_service import get_request_policy_store
from litellm.proxy.retail.request_policy_store import NativeRequestPolicyStore
from tests.unit.proxy.retail.test_request_policy_store import Client, Reader

PREFIX: Final = "/retail/admin/request-policies"


@pytest.fixture(autouse=True)
def dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("RETAIL_OPERATION_AUDIT_ENABLED", "RETAIL_NATIVE_AUDIT_ENABLED", "RETAIL_BILLING_ENABLED",
        "RETAIL_REQUIRE_COORDINATION_REDIS"):
        monkeypatch.setenv(name, "true")


@dataclass(frozen=True, slots=True)
class Context:
    app: FastAPI
    reader: Reader


def application(role: LitellmUserRoles = LitellmUserRoles.PROXY_ADMIN, *, custom: bool = False) -> Context:
    app: Final = FastAPI()
    reader: Final = Reader([])
    store: Final = NativeRequestPolicyStore(Client(reader))

    async def auth() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="admin-owner", user_role=role).model_copy(update={"authenticated_by_custom_auth": custom})

    def authority() -> NativeRequestPolicyStore:
        return store

    app.dependency_overrides[retail_admin] = auth
    app.dependency_overrides[get_request_policy_store] = authority
    app.include_router(router)
    return Context(app, reader)


@pytest.mark.asyncio
async def test_default_off_endpoint_returns_not_found_without_native_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RETAIL_REQUEST_POLICIES_ENABLED", raising=False)
    context: Final = application()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://fixture") as client:
        result: Final = await client.get(PREFIX+"/policies")
    assert result.status_code == 404 and not context.reader.queries


@pytest.mark.asyncio
@pytest.mark.parametrize("role,custom", ((LitellmUserRoles.PROXY_ADMIN, True),
    (LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY, False), (LitellmUserRoles.CUSTOMER, False)))
async def test_cached_non_native_or_non_admin_identity_cannot_use_management_api(monkeypatch: pytest.MonkeyPatch,
    role: LitellmUserRoles, custom: bool) -> None:
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", "true")
    context: Final = application(role, custom=custom)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://fixture") as client:
        result: Final = await client.get(PREFIX+"/policies")
    assert result.status_code == 403 and not context.reader.queries


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ({"name": "Prompt", "content": "private-prompt", "unknown": "private-input"},
    {"name": "Prompt", "content": "private-prompt", "enabled": "true"}))
async def test_validation_errors_do_not_echo_private_prompt_input(monkeypatch: pytest.MonkeyPatch, body: Mapping[str, object]) -> None:
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", "true")
    context: Final = application()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://fixture") as client:
        result: Final = await client.post(PREFIX+"/prompts", json=body, headers={"Idempotency-Key": "valid-operation"})
    assert result.status_code == 422
    assert "private-prompt" not in result.text and "private-input" not in result.text
    assert "no-store" in result.headers["cache-control"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ("/targets?kind=model&limit=101", "/targets?kind=deployment", "/policies?offset=10001",
    "/policies?limit=0", "/prompts/not-a-uuid"))
async def test_management_paths_and_pagination_are_bounded(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", "true")
    context: Final = application()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://fixture") as client:
        result: Final = await client.get(PREFIX+url)
    assert result.status_code == 422 and not context.reader.queries


@pytest.mark.asyncio
async def test_native_database_outage_is_generic_service_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", "true")
    context: Final = application()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://fixture") as client:
        result: Final = await client.get(PREFIX+"/prompts")
    assert result.status_code == 503 and "private-native-failure" not in result.text
    assert "no-store" in result.headers["cache-control"]
