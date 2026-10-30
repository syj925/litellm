from datetime import datetime, timezone
from decimal import Decimal
from typing import Final

import pytest

from litellm.litellm_core_utils.llm_cost_calc.tool_call_cost_tracking import StandardBuiltInToolCostTracking
from litellm.litellm_core_utils.llm_cost_calc.utils import calculate_image_response_cost_from_usage
from litellm.llms.anthropic.cost_calculation import get_cost_for_anthropic_web_search
from litellm.proxy.retail.catalog_expression import _token_policy
from litellm.proxy.retail.catalog_image_expression import (
    catalog_image_expression_policy,
    catalog_search_expression_policy,
)
from litellm.proxy.retail.expression_pricing import (
    ExpressionPricingError,
    evaluate_expression,
    freeze_expression_context,
    validate_expression_policy,
)
from litellm.proxy.retail.expression_usage import expression_usage_facts, request_usage_evidence
from litellm.types.utils import (
    ImageObject,
    ImageResponse,
    ImageUsage,
    ImageUsageInputTokensDetails,
    ModelInfo,
    ServerToolUse,
    Usage,
)

_START: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)
_PRICES: Final[ModelInfo] = {
    "mode": "image_generation", "litellm_provider": "gemini", "input_cost_per_token": 0.000002,
    "output_cost_per_token": 0.000008, "output_cost_per_image_token": 0.000016,
    "input_cost_per_image": 0.7, "output_cost_per_image": 0.04,
}


def _response(usage: ImageUsage | None) -> ImageResponse:
    return ImageResponse(data=[ImageObject(b64_json="first"), ImageObject(b64_json="second")], usage=usage)


@pytest.mark.parametrize("breakdown", (True, False))
def test_image_token_usage_matches_native_without_adding_alternative_flat_fees(breakdown: bool) -> None:
    usage: Final = ImageUsage(
        input_tokens=10, output_tokens=25, total_tokens=35,
        input_tokens_details=ImageUsageInputTokensDetails(text_tokens=10, image_tokens=0),
        **({"output_tokens_details": {"text_tokens": 5, "image_tokens": 20}} if breakdown else {}),
    )
    response: Final = _response(usage)
    policy: Final = validate_expression_policy(catalog_image_expression_policy("fixture", _PRICES, _token_policy))
    facts: Final = expression_usage_facts(response, policy, request_usage_evidence({}, "image_generation"))
    billed: Final = evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))
    native: Final = calculate_image_response_cost_from_usage("fixture", response, "gemini", _PRICES)
    assert Decimal(str(billed["amount"])) == pytest.approx(Decimal(str(native)), abs=Decimal("1e-15"))
    assert "image token usage" in billed["tiers"]


@pytest.mark.parametrize("usage", (
    None,
    ImageUsage(
        input_tokens=0, output_tokens=0, total_tokens=0,
        input_tokens_details=ImageUsageInputTokensDetails(text_tokens=0, image_tokens=0),
    ),
))
def test_no_token_usage_uses_returned_image_count_only(usage: ImageUsage | None) -> None:
    response: Final = _response(usage)
    policy: Final = validate_expression_policy(catalog_image_expression_policy("fixture", _PRICES, _token_policy))
    facts: Final = expression_usage_facts(response, policy, request_usage_evidence({}, "image_generation"))
    billed: Final = evaluate_expression(policy, facts, freeze_expression_context(policy, {"n": 9}, _START))
    assert calculate_image_response_cost_from_usage("fixture", response, "gemini", _PRICES) is None
    assert Decimal(str(billed["amount"])) == Decimal("0.08")
    assert billed["tiers"] == ["returned images"]


def test_explicit_zero_token_prices_do_not_activate_nonzero_flat_fallback() -> None:
    prices: Final = {**_PRICES, "input_cost_per_token": 0, "output_cost_per_token": 0, "output_cost_per_image_token": 0}
    policy: Final = validate_expression_policy(catalog_image_expression_policy("fixture", prices, _token_policy))
    facts: Final = {"tokens": 1, "p": 1, "c": 0, "img_o": 0, "len": 1, "cr": 0, "cc": 0}
    billed: Final = evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))
    assert Decimal(str(billed["amount"])) == 0
    assert "image token usage" in billed["tiers"]


@pytest.mark.parametrize(("unit", "expected"), (("per_query", "0.122"), ("per_prompt", "0.094")))
def test_grounding_is_added_once_outside_image_alternative(unit: str, expected: str) -> None:
    prices: Final = {
        **_PRICES, "web_search_billing_unit": unit,
        "search_context_cost_per_query": {"search_context_size_low": 0.001, "search_context_size_medium": 0.014},
    }
    policy: Final = validate_expression_policy(catalog_image_expression_policy("fixture", prices, _token_policy))
    facts: Final = {"usage": {"images": "2", "web_search_requests": "3"}}
    billed: Final = evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))
    assert Decimal(str(billed["amount"])) == Decimal(expected)
    assert policy.usage_bounds["web_search_requests"] == "64"
    with pytest.raises(ExpressionPricingError, match="Missing"):
        evaluate_expression(policy, {"usage": {"images": "2"}}, freeze_expression_context(policy, {}, _START))


@pytest.mark.parametrize("extra", (
    {"output_cost_per_pixel": "0.01"},
    {"search_context_cost_per_query": {"search_context_size_low": "0.01"}},
    {"search_context_cost_per_query": {"search_context_size_medium": "invalid"}},
    {"regional_endpoint_uplift_multiplier": "1.1"},
    {"regional_processing_uplift_multiplier_eu": "1.1"},
    {"search_free_requests": {"count": 100, "period": "day"}},
    {"per_request_fee": "0.1"},
))
def test_unknown_or_unrepresentable_cost_dimensions_are_not_dropped(extra: dict[str, object]) -> None:
    assert catalog_image_expression_policy("fixture", {**_PRICES, **extra}, _token_policy) is None


def test_chat_grounding_preserves_token_cache_and_existing_usage_bounds() -> None:
    original: Final = {
        "version": "expression-v1",
        "expression": 'tier("chat", p * 2 + c * 8 + cache_write("750") * 3 + fixed(u("requests") * 0.01))',
        "timezone": "Asia/Shanghai", "usage_bounds": {"requests": "1"},
    }
    metadata: Final = {
        "litellm_provider": "gemini", "search_context_cost_per_query": {"search_context_size_medium": "0.014"},
        "web_search_billing_unit": "per_query",
    }
    policy: Final = validate_expression_policy(catalog_search_expression_policy(original, metadata))
    facts: Final = {
        "p": 1000, "c": 100, "cache_write_ttl_tokens": {"750": 10},
        "usage": {"requests": "1", "web_search_requests": "3"},
    }
    billed: Final = evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))
    assert Decimal(str(billed["amount"])) == Decimal("0.05483")
    assert billed["tiers"] == ["chat"]
    assert policy.timezone == original["timezone"]
    assert policy.usage_bounds == {"requests": "1", "web_search_requests": "64"}


@pytest.mark.parametrize(("unit", "expected"), (("per_query", "2.075"), ("per_prompt", "2.025")))
def test_google_maps_uses_reported_units_and_configured_rate_without_default_prices(unit: str, expected: str) -> None:
    original: Final = {"version": "expression-v1", "expression": "p * 2", "timezone": "UTC", "usage_bounds": {}}
    metadata: Final = {
        "litellm_provider": "gemini", "google_maps_grounding_cost_per_query": "0.025", "web_search_billing_unit": unit,
    }
    policy: Final = validate_expression_policy(catalog_search_expression_policy(original, metadata))
    facts: Final = {"p": 1000000, "usage": {"google_maps_grounding_requests": "3"}}
    result: Final = evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))
    assert Decimal(str(result["amount"])) == Decimal(expected)


@pytest.mark.parametrize("provider", ("openai", "anthropic"))
@pytest.mark.parametrize("requests", (0, 3, 64))
def test_openai_and_anthropic_search_charge_actual_reported_requests_like_native(
    provider: str, requests: int,
) -> None:
    prices: Final[ModelInfo] = {"search_context_cost_per_query": {
        "search_context_size_low": 0.014, "search_context_size_medium": 0.014, "search_context_size_high": 0.014,
    }}
    original: Final = {"version": "expression-v1", "expression": "0", "timezone": "UTC", "usage_bounds": {}}
    policy: Final = validate_expression_policy(catalog_search_expression_policy(
        original, {**prices, "litellm_provider": provider},
    ))
    facts: Final = {"usage": {"web_search_requests": str(requests)}}
    billed: Final = evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))
    native: Final = (
        get_cost_for_anthropic_web_search(
            model_info=prices, usage=Usage(server_tool_use=ServerToolUse(web_search_requests=requests)),
        ) if provider == "anthropic"
        else StandardBuiltInToolCostTracking.get_cost_for_web_search(model_info=prices) * requests
    )
    assert Decimal(str(billed["amount"])) == Decimal("0.014") * requests
    assert float(str(billed["amount"])) == pytest.approx(native)
    assert billed["usage"] == {"web_search_requests": str(requests)}


@pytest.mark.parametrize("rate", ("0", "0.010000000000000000000001"))
def test_search_expression_preserves_configured_decimal_precision_and_requires_reported_usage(rate: str) -> None:
    metadata: Final = {"litellm_provider": "anthropic", "search_context_cost_per_query": {
        "search_context_size_medium": rate,
    }}
    original: Final = {"version": "expression-v1", "expression": "0", "timezone": "UTC", "usage_bounds": {}}
    policy: Final = validate_expression_policy(catalog_search_expression_policy(original, metadata))
    context: Final = freeze_expression_context(policy, {}, _START)
    billed: Final = evaluate_expression(policy, {"usage": {"web_search_requests": "3"}}, context)
    assert Decimal(str(billed["amount"])) == Decimal(rate) * 3
    with pytest.raises(ExpressionPricingError, match="Missing"):
        evaluate_expression(policy, {"usage": {}}, context)


def test_anthropic_search_uses_native_medium_rate_even_when_other_context_rates_differ() -> None:
    original: Final = {"version": "expression-v1", "expression": "0", "timezone": "UTC", "usage_bounds": {}}
    metadata: Final = {"litellm_provider": "anthropic", "search_context_cost_per_query": {
        "search_context_size_low": "0.001", "search_context_size_medium": "0.014", "search_context_size_high": "0.05",
    }}
    policy: Final = validate_expression_policy(catalog_search_expression_policy(original, metadata))
    billed: Final = evaluate_expression(
        policy, {"usage": {"web_search_requests": "3"}}, freeze_expression_context(policy, {}, _START),
    )
    assert Decimal(str(billed["amount"])) == Decimal("0.042")


@pytest.mark.parametrize("configured", (
    {"search_context_size_low": "0.001", "search_context_size_medium": "0.014", "search_context_size_high": "0.05"},
    {"search_context_size_medium": "0.014"},
    {"search_context_size_low": "0.014", "search_context_size_medium": "0.014", "search_context_size_high": "NaN"},
))
def test_openai_search_context_variants_require_review_when_one_reported_counter_cannot_select_the_rate(
    configured: dict[str, str],
) -> None:
    original: Final = {"version": "expression-v1", "expression": "0", "timezone": "UTC", "usage_bounds": {}}
    assert catalog_search_expression_policy(original, {
        "litellm_provider": "openai", "search_context_cost_per_query": configured,
    }) is None
