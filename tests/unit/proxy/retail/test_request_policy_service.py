import asyncio
from typing import Final

import pytest
from fastapi import HTTPException

from litellm.proxy.retail.request_policy_service import (
    close_request_policy_service,
    get_request_policy_store,
    start_request_policy_service,
)


@pytest.mark.asyncio
async def test_disabled_policy_service_allocates_no_background_work(monkeypatch: pytest.MonkeyPatch) -> None:
    await close_request_policy_service()
    monkeypatch.delenv("RETAIL_REQUEST_POLICIES_ENABLED", raising=False)
    before: Final = frozenset(asyncio.all_tasks())
    await start_request_policy_service()
    assert frozenset(asyncio.all_tasks()) == before
    with pytest.raises(HTTPException) as failure:
        get_request_policy_store()
    assert failure.value.status_code == 503
    await close_request_policy_service()


def test_policy_getter_rejects_enabled_but_uninitialized_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RETAIL_OPERATION_AUDIT_ENABLED", "RETAIL_NATIVE_AUDIT_ENABLED", "RETAIL_REQUIRE_COORDINATION_REDIS",
        "RETAIL_BILLING_ENABLED", "RETAIL_REQUEST_POLICIES_ENABLED",
    ):
        monkeypatch.setenv(name, "true")
    with pytest.raises(HTTPException) as failure:
        get_request_policy_store()
    assert failure.value.status_code == 503 and "authority" in str(failure.value.detail)
