import asyncio
from datetime import datetime, timedelta, timezone
from typing import Final

import httpx
import pytest
from fastapi import FastAPI

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

from litellm.proxy.retail.catalog_source_data import ExternalMetadataRecord
from litellm.proxy.retail.catalog_sources import (
    SourceFetchFailure,
    SourceFetchSuccess,
    _Snapshot,
    _download,
    fetch_source,
    retained_snapshot,
    router,
)

_NOW: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _record(identifier: str = "lab/model") -> ExternalMetadataRecord:
    return ExternalMetadataRecord(
        id=identifier, source="models_dev", source_url="https://models.dev/models.json", retrieved_at=_NOW
    )


def test_failed_fetch_retains_last_good_records_and_success_timestamp():
    previous: Final = _Snapshot(source="models_dev", records=(_record(),), last_success_at=_NOW)
    attempted: Final = _NOW + timedelta(hours=1)
    result: Final = retained_snapshot(previous, SourceFetchFailure(error="The source could not be reached"), attempted)
    assert result.records is previous.records
    assert result.last_success_at == previous.last_success_at
    assert result.last_attempt_at == attempted
    assert result.last_error == "The source could not be reached"


def test_successful_refresh_replaces_records_and_clears_previous_error():
    previous: Final = _Snapshot(source="models_dev", records=(_record(),), last_error="previous failure")
    replacement: Final = SourceFetchSuccess(records=(_record("lab/new"),))
    result: Final = retained_snapshot(previous, replacement, _NOW)
    assert result.records == replacement.records
    assert result.last_success_at == _NOW
    assert result.last_error is None


def test_unexpected_catalog_shrink_retains_last_good_data():
    previous: Final = _Snapshot(
        source="models_dev", records=tuple(_record(f"lab/{index}") for index in range(20)), last_success_at=_NOW
    )
    result: Final = retained_snapshot(previous, SourceFetchSuccess(records=(_record(),)), _NOW)
    assert result.records is previous.records
    assert result.last_error == "The source returned an unexpectedly small catalog"


@pytest.mark.asyncio
async def test_one_source_http_failure_does_not_discard_the_other_source_result():
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(503, text="Private diagnostic that must not escape")
        if request.url.path == "/models.json":
            return httpx.Response(200, json={"lab/model": {"name": "Model"}})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model_result, router_result = await asyncio.gather(
            fetch_source(client, "models_dev", _NOW), fetch_source(client, "openrouter", _NOW)
        )
    assert isinstance(model_result, SourceFetchSuccess)
    assert model_result.records[0].id == "lab/model"
    assert isinstance(router_result, SourceFetchFailure)
    assert router_result.error == "The source could not be reached"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://elsewhere.invalid"}),
        httpx.Response(200, text="not JSON"),
        httpx.Response(200, json={"data": []}),
    ],
)
async def test_redirects_invalid_json_and_empty_catalog_are_rejected(response: httpx.Response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        result: Final = await fetch_source(client, "openrouter", _NOW)
    assert isinstance(result, SourceFetchFailure)


@pytest.mark.asyncio
async def test_response_size_limit_is_enforced_before_json_is_adopted():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))
    ) as client:
        result: Final = await _download(client, "https://openrouter.ai/api/v1/models", max_bytes=3)
    assert isinstance(result, SourceFetchFailure)
    assert result.error == "The source response exceeded the size limit"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("GET", "/retail/admin/catalog-sources"),
                                         ("POST", "/retail/admin/catalog-sources/sync")])
@pytest.mark.parametrize("role", [LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY])
async def test_catalog_source_operations_reject_customers_and_readonly_admins(method: str, path: str, role: str):
    app: Final = FastAPI()
    app.include_router(router, prefix="/retail")

    async def authenticated_user() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="test-account", user_role=role)

    app.dependency_overrides[user_api_key_auth] = authenticated_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.request(method, path)
    assert response.status_code == 403
    assert "administrator permission" in response.text
