from datetime import datetime, timedelta, timezone
from typing import Final

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail.api import retail_store, retail_user
from litellm.proxy.retail.cache_usage_metrics import (
    CacheUsageMetrics,
    CacheUsageQuery,
    cache_usage_owner,
    cache_usage_window,
    router,
)
from litellm.proxy.retail.store import WalletStore

_START: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _query(**filters: object) -> CacheUsageQuery:
    return CacheUsageQuery.model_validate({"start_date": _START, "end_date": _START + timedelta(days=1), **filters})


def test_window_preserves_exact_instants_and_accepts_a_full_31_days() -> None:
    offset: Final = timezone(timedelta(hours=8))
    start, end = cache_usage_window(_query(start_date=_START.astimezone(offset), end_date=_START + timedelta(days=31)))
    assert start == _START.replace(tzinfo=None)
    assert end - start == timedelta(days=31)


@pytest.mark.parametrize("delta", (timedelta(), timedelta(seconds=-1), timedelta(days=31, microseconds=1)))
def test_invalid_windows_do_not_expand_or_fall_back_to_default(delta: timedelta) -> None:
    with pytest.raises(HTTPException) as error:
        cache_usage_window(_query(end_date=_START + delta))
    assert error.value.status_code == 422


def test_dates_without_offsets_are_rejected() -> None:
    with pytest.raises(HTTPException) as error:
        cache_usage_window(_query(start_date=_START.replace(tzinfo=None)))
    assert error.value.status_code == 422


@pytest.mark.parametrize("role", (LitellmUserRoles.PROXY_ADMIN, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY))
def test_global_administrators_can_select_all_users_or_one_exact_user(role: LitellmUserRoles) -> None:
    admin: Final = UserAPIKeyAuth(user_id="admin", user_role=role)
    assert cache_usage_owner(_query(scope="global"), admin) is None
    assert cache_usage_owner(_query(scope="global", user_id="selected"), admin) == "selected"
    assert cache_usage_owner(_query(scope="self"), admin) == "admin"


@pytest.mark.parametrize("scope,user_id", (("global", None), ("global", "owner"), ("self", "other")))
def test_personal_scope_cannot_read_global_or_another_account(scope: str, user_id: str | None) -> None:
    user: Final = UserAPIKeyAuth(user_id="owner", user_role=LitellmUserRoles.INTERNAL_USER)
    with pytest.raises(HTTPException) as error:
        cache_usage_owner(_query(scope=scope, user_id=user_id), user)
    assert error.value.status_code == 403


def test_personal_scope_uses_the_authenticated_account_when_no_user_filter_is_supplied() -> None:
    user: Final = UserAPIKeyAuth(user_id="owner", user_role=LitellmUserRoles.INTERNAL_USER)
    assert cache_usage_owner(_query(), user) == "owner"
    assert cache_usage_owner(_query(user_id="owner"), user) == "owner"


def test_response_preserves_known_ttl_parts_and_unknown_counts_without_raw_fields() -> None:
    metrics: Final = CacheUsageMetrics.model_validate({
        "cache_read_tokens": 10, "cache_write_tokens": 30,
        "cache_write_ttl_tokens": {"300": 11, "750": 12}, "cache_write_unclassified_tokens": 7,
        "request_count": 3, "cache_usage_samples": 2, "cache_usage_unknown_requests": 1,
        "api_key": "private", "user_id": "private", "spend": "private", "metadata": {"secret": "private"},
    })
    assert metrics.cache_write_tokens == sum(metrics.cache_write_ttl_tokens.values()) + 7
    assert metrics.request_count == metrics.cache_usage_samples + metrics.cache_usage_unknown_requests
    assert set(metrics.model_dump()) == {
        "cache_read_tokens", "cache_write_tokens", "cache_write_ttl_tokens", "cache_write_unclassified_tokens",
        "request_count", "cache_usage_samples", "cache_usage_unknown_requests",
    }
    assert CacheUsageMetrics().cache_write_unclassified_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("query,status", (
    ({"scope": "global"}, 403),
    ({"scope": "self", "user_id": "other"}, 403),
    ({"scope": "team"}, 422),
    ({"organization_id": "ignored-filter"}, 422),
    ({"public_models": "true"}, 422),
    ({"models": ""}, 422),
    ({"start_date": "2026-01-01", "end_date": "2026-01-03"}, 422),
    ({"end_date": "2026-03-01T00:00:00Z"}, 422),
))
async def test_endpoint_rejects_unsupported_or_unauthorized_filters_before_reading_storage(
    query: dict[str, str], status: int,
) -> None:
    async def own_user() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="owner", user_role=LitellmUserRoles.INTERNAL_USER)

    async def disconnected_store() -> WalletStore:
        return WalletStore("postgresql://unreachable.invalid/unused")

    app: Final = FastAPI()
    app.include_router(router, prefix="/retail")
    app.dependency_overrides[retail_user] = own_user
    app.dependency_overrides[retail_store] = disconnected_store
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retail.test") as client:
        response: Final = await client.get("/retail/cache-usage", params={
            "start_date": "2026-01-01T00:00:00Z", "end_date": "2026-01-03T00:00:00Z", **query,
        })
    assert response.status_code == status
