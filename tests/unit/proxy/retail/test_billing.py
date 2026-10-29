from datetime import datetime, timezone
from typing import Final

import pytest
from fastapi import HTTPException

from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail.billing import RetailBilling, price_snapshot, reservation_id
from litellm.proxy.retail.pricing import UsageFacts, charge_for_usage


@pytest.mark.parametrize(
    ("params", "kwargs"),
    (
        ({"no-log": True}, None),
        (
            {"no-log": True, "proxy_server_request": {"headers": {"x-litellm-disable-callbacks": "retail_wallet"}}},
            {"litellm_disabled_callbacks": ["retail_wallet"]},
        ),
        (
            {"proxy_server_request": {"headers": {"x-litellm-disable-callbacks": "retail_wallet"}}},
            {"litellm_disabled_callbacks": ["retail_wallet"]},
        ),
    ),
)
def test_native_logging_controls_cannot_disable_the_wallet_accounting_callback(
    params: dict[str, object], kwargs: dict[str, object] | None
) -> None:
    logging: Final = Logging(
        model="test-model",
        messages=[{"role": "user", "content": "test"}],
        stream=True,
        call_type="acompletion",
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        litellm_call_id="call-1",
        function_id="function-1",
        kwargs=kwargs,
    )
    assert logging.should_run_callback(RetailBilling(), params, "async_success_handler") is True


@pytest.mark.parametrize(
    "data",
    (
        {"metadata": {"retail_request_id": "reserved-request"}},
        {"litellm_metadata": {"retail_request_id": "reserved-request"}},
        {"litellm_params": {"metadata": {"retail_request_id": "reserved-request"}}},
        {"standard_logging_object": {"metadata": {"retail_request_id": "reserved-request"}}},
    ),
)
def test_native_callback_metadata_recovers_the_original_reservation(data: dict[str, object]) -> None:
    assert reservation_id(data) == "reserved-request"


@pytest.mark.parametrize("data", ({}, {"metadata": {"retail_request_id": ""}}, {"metadata": {"retail_request_id": 1}}))
def test_missing_or_malformed_reservation_is_not_treated_as_a_billable_request(data: dict[str, object]) -> None:
    assert reservation_id(data) is None


@pytest.mark.asyncio
async def test_administrator_inference_bypasses_retail_wallet_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    callback: Final = RetailBilling()
    data: Final[dict[str, object]] = {"model": "test-model", "messages": []}
    admin: Final = UserAPIKeyAuth(user_id="admin", user_role=LitellmUserRoles.PROXY_ADMIN)
    assert await callback.async_pre_call_hook(admin, None, data, "acompletion") is data


@pytest.mark.asyncio
async def test_disabled_retail_billing_preserves_native_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "false")
    callback: Final = RetailBilling()
    data: Final[dict[str, object]] = {"model": "test-model", "messages": []}
    user: Final = UserAPIKeyAuth(user_id="reader", user_role=LitellmUserRoles.INTERNAL_USER)
    assert await callback.async_pre_call_hook(user, None, data, "acompletion") is data


@pytest.mark.asyncio
@pytest.mark.parametrize("call_type", ("aimage_generation", "atranscription", "afine_tuning", "unknown_endpoint"))
async def test_unpriced_endpoint_types_fail_before_wallet_or_provider_access(
    call_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    callback: Final = RetailBilling()
    user: Final = UserAPIKeyAuth(user_id="reader", user_role=LitellmUserRoles.INTERNAL_USER)
    with pytest.raises(HTTPException) as failure:
        await callback.async_pre_call_hook(user, None, {"model": "test-model"}, call_type)
    assert failure.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("background", "batch"))
async def test_async_execution_cannot_bypass_prepaid_request_lifecycle(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    callback: Final = RetailBilling()
    user: Final = UserAPIKeyAuth(user_id="reader", user_role=LitellmUserRoles.INTERNAL_USER)
    with pytest.raises(HTTPException) as failure:
        await callback.async_pre_call_hook(user, None, {"model": "test-model", field: True}, "aresponses")
    assert failure.value.status_code == 400


@pytest.mark.asyncio
async def test_server_priced_tools_require_an_explicit_supported_price_card(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    callback: Final = RetailBilling()
    user: Final = UserAPIKeyAuth(user_id="reader", user_role=LitellmUserRoles.INTERNAL_USER)
    with pytest.raises(HTTPException) as failure:
        await callback.async_pre_call_hook(
            user, None, {"model": "test-model", "tools": [{"type": "web_search"}]}, "aresponses"
        )
    assert failure.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ({"n": 2}, {"best_of": 5}, {"n": True}, {"n": 0}, {"prompt": ["a", "b"]}))
async def test_multiple_generated_candidates_cannot_expand_a_single_candidate_reservation(
    data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    user: Final = UserAPIKeyAuth(user_id="reader", user_role=LitellmUserRoles.INTERNAL_USER)
    with pytest.raises(HTTPException) as failure:
        await RetailBilling().async_pre_call_hook(user, None, {"model": "test-model", **data}, "acompletion")
    assert failure.value.status_code == 400


def test_pricing_snapshot_keeps_the_model_and_plan_from_the_same_discount_decision() -> None:
    card: Final = {
        "model": "model-a",
        "version": 3,
        "input_cost_per_token": "0.000002",
        "output_cost_per_token": "0.000010",
        "cache_read_input_token_cost": "0.0000002",
        "cache_creation_input_token_cost": "0.0000025",
    }
    decision: Final = {"model": "model-a", "plan_id": "plan-b", "discount": "0.7", "discount_source": "customer_model"}
    saved: Final = price_snapshot(card, decision, "acompletion")
    current: Final = price_snapshot(card, {**decision, "plan_id": "plan-c", "discount": "0.8"}, "acompletion")
    assert saved["plan_id"] == "plan-b"
    assert saved["discount_model"] == "model-a"
    assert saved["discount_source"] == "customer_model"
    assert saved["price_version"] == 3
    assert saved["discount"] == saved["multiplier"] == "0.7"
    assert charge_for_usage(saved, UsageFacts(input_tokens=10, output_tokens=1)) == "0.000021000"
    assert charge_for_usage(current, UsageFacts(input_tokens=10, output_tokens=1)) == "0.000024000"
    assert saved["discount"] == "0.7"
