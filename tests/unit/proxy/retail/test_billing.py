from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Final

import pytest
from fastapi import HTTPException

from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail.billing import RetailBilling, _request_output_limit, price_snapshot, reservation_id
from litellm.proxy.retail.pool_routing import filter_pool_deployments, guard_pool_deployment, reset_pool_routing
from litellm.proxy.retail.pool_store import (
    PoolAccess,
    PoolGrant,
    PoolRouteDecision,
    PoolStore,
    _PoolRow,
    _Settings,
    _StoredBinding,
    allowed_model_names,
    choose_route,
)
from litellm.proxy.retail.pricing import UsageFacts, charge_for_usage
from litellm.proxy.retail.request_tracing import TraceCollector, activate_trace, reset_trace


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
async def test_billing_trace_records_the_actual_bypass_without_claiming_a_wallet_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    trace: Final = TraceCollector("POST", "/v1/chat/completions")
    token: Final = activate_trace(trace)
    data: Final[dict[str, object]] = {"model": "fixture", "litellm_call_id": "bypass-call"}
    try:
        admin: Final = UserAPIKeyAuth(user_id="admin", user_role=LitellmUserRoles.PROXY_ADMIN)
        assert await RetailBilling().async_pre_call_hook(admin, None, data, "acompletion") is data
    finally:
        reset_trace(token)
    result: Final = trace.snapshot()
    assert result["call_id"] == "bypass-call"
    assert result["retail_request_id"] is None
    assert result["events"][0]["name"] == "billing.bypassed"
    assert result["phases"][0]["name"] == "billing.prepare"
    assert result["phases"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_billing_validation_failures_close_the_observed_phase_without_a_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    trace: Final = TraceCollector("POST", "/unsupported")
    token: Final = activate_trace(trace)
    try:
        user: Final = UserAPIKeyAuth(user_id="reader", user_role=LitellmUserRoles.INTERNAL_USER)
        with pytest.raises(HTTPException, match="not enabled"):
            await RetailBilling().async_pre_call_hook(user, None, {"model": "fixture"}, "unsupported")
    finally:
        reset_trace(token)
    result: Final = trace.snapshot()
    assert result["retail_request_id"] is None
    assert result["phases"][0]["name"] == "billing.prepare"
    assert result["phases"][0]["status"] == "error"


@pytest.mark.asyncio
async def test_disabled_retail_billing_preserves_native_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "false")
    callback: Final = RetailBilling()
    data: Final[dict[str, object]] = {"model": "test-model", "messages": []}
    user: Final = UserAPIKeyAuth(user_id="reader", user_role=LitellmUserRoles.INTERNAL_USER)
    assert await callback.async_pre_call_hook(user, None, data, "acompletion") is data


@pytest.mark.asyncio
@pytest.mark.parametrize("call_type", ("afine_tuning", "unknown_endpoint", "_arealtime"))
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
@pytest.mark.parametrize(("field", "value"), (
    ("background", True), ("batch", True), ("service_tier", "batch"), ("service_tier", "batches"),
))
async def test_async_execution_cannot_bypass_prepaid_request_lifecycle(
    field: str, value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    callback: Final = RetailBilling()
    user: Final = UserAPIKeyAuth(user_id="reader", user_role=LitellmUserRoles.INTERNAL_USER)
    with pytest.raises(HTTPException) as failure:
        await callback.async_pre_call_hook(user, None, {"model": "test-model", field: value}, "aresponses")
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


@pytest.mark.parametrize(("call_type", "field"), (("acompletion", "max_tokens"),
                                                 ("acompletion", "max_completion_tokens"),
                                                 ("aresponses", "max_output_tokens")))
def test_expression_output_limit_rejects_request_above_frozen_capacity(call_type: str, field: str) -> None:
    snapshot: Final = {"version": "expression-v1", "expression_limits": {"output_tokens": 512}}
    with pytest.raises(HTTPException, match="frozen expression capacity") as failure:
        _request_output_limit(snapshot, {field: 513}, call_type)
    assert failure.value.status_code == 400
    assert _request_output_limit(snapshot, {field: 512}, call_type) == 512


@pytest.mark.parametrize("call_type", ("acompletion", "aresponses"))
def test_expression_default_output_fits_frozen_capacity_while_explicit_smaller_limit_is_preserved(call_type: str) -> None:
    snapshot: Final = {"version": "expression-v1", "expression_limits": {"output_tokens": 512}}
    field: Final = "max_output_tokens" if call_type == "aresponses" else "max_tokens"
    assert _request_output_limit(snapshot, {}, call_type) == 512
    assert _request_output_limit(snapshot, {field: None}, call_type) == 512
    assert _request_output_limit(snapshot, {field: 128}, call_type) == 128


@pytest.mark.parametrize("call_type", ("aimage_generation", "aimage_edit", "atranscription", "aspeech"))
def test_media_quote_uses_frozen_output_capacity_instead_of_text_default_or_requested_limit(call_type: str) -> None:
    snapshot: Final = {"version": "expression-v1", "expression_limits": {"output_tokens": 2048}}
    assert _request_output_limit(snapshot, {}, call_type) == 2048
    assert _request_output_limit(snapshot, {"max_tokens": 100}, call_type) == 2048


def test_legacy_output_defaults_and_embedding_zero_limit_are_preserved() -> None:
    legacy: Final = {"version": "token-v3"}
    assert _request_output_limit(legacy, {}, "acompletion") == 1024
    assert _request_output_limit(legacy, {"max_tokens": 500000}, "acompletion") == 500000
    assert _request_output_limit({"version": "expression-v1", "expression_limits": {"output_tokens": 512}},
                                 {}, "aembedding") == 0


@pytest.mark.asyncio
async def test_administrator_model_discovery_keeps_native_results_without_retail_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    admin: Final = UserAPIKeyAuth(user_id="admin", user_role=LitellmUserRoles.PROXY_ADMIN)
    models: Final = ("shared", "other", "retry")
    assert await RetailBilling().async_filter_listed_models(admin, models) == models


@pytest.mark.asyncio
async def test_disabled_retail_model_discovery_preserves_native_key_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "false")
    user: Final = UserAPIKeyAuth(user_id="reader", user_role=LitellmUserRoles.INTERNAL_USER)
    models: Final = ("shared", "other", "retry")
    assert await RetailBilling().async_filter_listed_models(user, models) == models


class _MemoryPools(PoolStore):
    def __init__(self, enabled: bool = True, has_bindings: bool = True, access: PoolAccess | None = None) -> None:
        self.access = access if access is not None else PoolAccess()
        self.pools = (
            _PoolRow(
                id="aws", name="AWS resource", source_kind="bedrock", enabled=enabled,
                visibility="public", version=1, updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            _PoolRow(
                id="direct", name="Direct resource", source_kind="anthropic", enabled=True,
                visibility="public", version=1, updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        )
        self.bindings = (
            (_StoredBinding(pool_id="aws", deployment_id="aws-deployment", model="shared"),)
            if has_bindings else ()
        ) + (
            _StoredBinding(pool_id="direct", deployment_id="direct-deployment", model="shared"),
            _StoredBinding(pool_id="direct", deployment_id="foreign-deployment", model="foreign"),
        )

    async def resolve_route(
        self, user_id: str, model: str, policy: object | None, deployments: Sequence[Mapping[str, object]],
    ) -> PoolRouteDecision | None:
        return choose_route(
            _Settings(routing_enabled=False, version=1), self.pools, self.bindings, self.access,
            model, policy, deployments,
        )

    async def key_model_names(
        self, user_id: str, policy: object | None, deployments: Sequence[Mapping[str, object]],
    ) -> frozenset[str] | None:
        return allowed_model_names(self.access, self.pools, self.bindings, policy, deployments)


def _pooled_deployments() -> tuple[Mapping[str, object], ...]:
    return tuple(
        {
            "model_name": model, "model_info": {"id": deployment_id},
            "litellm_params": {"model": "anthropic/" + model},
        }
        for deployment_id, model in (
            ("aws-deployment", "shared"), ("direct-deployment", "shared"), ("foreign-deployment", "foreign"),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "visible"),
    (
        ({"mode": "fixed", "pool_id": "aws"}, ("shared",)),
        ({"mode": "custom", "grants": [{"pool_id": "aws", "models": ["shared"]}]}, ("shared",)),
        ({"mode": "auto"}, ("shared", "foreign")),
    ),
)
@pytest.mark.parametrize(
    ("role", "billing_enabled"),
    (
        (LitellmUserRoles.PROXY_ADMIN, "true"),
        (LitellmUserRoles.PROXY_ADMIN, "false"),
        (LitellmUserRoles.INTERNAL_USER, "false"),
    ),
)
async def test_explicit_pool_policy_enforces_routing_and_discovery_despite_wallet_exemption(
    policy: dict[str, object], visible: tuple[str, ...], role: LitellmUserRoles,
    billing_enabled: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", billing_enabled)
    callback: Final = RetailBilling(pool_store=_MemoryPools(), deployments=_pooled_deployments)
    user: Final = UserAPIKeyAuth(
        user_id="owner", user_role=role, metadata={"retail_resource_policy": policy},
    )
    request: Final[dict[str, object]] = {
        "model": "shared", "metadata": {"retail_resource_pool_id": "forged", "retail_request_id": "forged"},
    }
    result: Final = await callback.async_pre_call_hook(user, None, request, "acompletion")
    assert result["metadata"] == result["litellm_metadata"] == {
        "retail_resource_pool_id": "aws", "retail_resource_pool_name": "AWS resource",
        "retail_resource_source_kind": "bedrock",
    }
    assert result["cache"] == {"namespace": "retail:owner:aws"}
    assert reservation_id(result) is None
    assert await callback.async_filter_listed_models(user, ("shared", "foreign")) == visible
    assert [item["model_info"] for item in filter_pool_deployments(_pooled_deployments())] == [
        {"id": "aws-deployment"},
    ]
    with pytest.raises(HTTPException, match="outside"):
        guard_pool_deployment({"model_info": {"id": "direct-deployment"}})
    reset_pool_routing()


@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "has_bindings", "status"), ((False, True, 403), (True, False, 503)))
async def test_exempt_key_cannot_fall_back_from_a_disabled_or_empty_fixed_pool(
    enabled: bool, has_bindings: bool, status: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "false")
    callback: Final = RetailBilling(
        pool_store=_MemoryPools(enabled=enabled, has_bindings=has_bindings), deployments=_pooled_deployments,
    )
    admin: Final = UserAPIKeyAuth(
        user_id="owner", user_role=LitellmUserRoles.PROXY_ADMIN,
        metadata={"retail_resource_policy": {"mode": "fixed", "pool_id": "aws"}},
    )
    with pytest.raises(HTTPException) as failure:
        await callback.async_pre_call_hook(admin, None, {"model": "shared"}, "acompletion")
    assert failure.value.status_code == status


@pytest.mark.asyncio
async def test_explicit_null_policy_cannot_be_treated_as_an_unrestricted_administrator_key() -> None:
    admin: Final = UserAPIKeyAuth(
        user_id="owner", user_role=LitellmUserRoles.PROXY_ADMIN, metadata={"retail_resource_policy": None},
    )
    callback: Final = RetailBilling()
    with pytest.raises(HTTPException) as inference:
        await callback.async_pre_call_hook(admin, None, {"model": "shared"}, "acompletion")
    with pytest.raises(HTTPException) as discovery:
        await callback.async_filter_listed_models(admin, ("shared",))
    assert inference.value.status_code == discovery.value.status_code == 403


@pytest.mark.asyncio
async def test_customer_without_key_policy_cannot_bypass_account_pools_when_global_routing_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litellm.constants import UI_SESSION_TOKEN_TEAM_ID

    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "true")
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "false")
    access: Final = PoolAccess(mode="custom", grants=(PoolGrant(pool_id="aws"),))
    callback: Final = RetailBilling(pool_store=_MemoryPools(access=access), deployments=_pooled_deployments)
    user: Final = UserAPIKeyAuth(
        user_id="owner", user_role=LitellmUserRoles.INTERNAL_USER, team_id=UI_SESSION_TOKEN_TEAM_ID,
    )
    assert await callback.async_filter_listed_models(user, ("shared", "foreign")) == ("shared",)
    with pytest.raises(HTTPException) as failure:
        await callback.async_pre_call_hook(user, None, {"model": "foreign"}, "acompletion")
    assert failure.value.status_code == 403
    reset_pool_routing()
