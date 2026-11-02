from datetime import datetime, timezone
from decimal import Decimal
from typing import Final

import pytest

from litellm.proxy.retail.catalog_pricing_metadata import (
    apply_token_modifiers,
    catalog_pricing_note,
    supported_pricing_metadata,
)
from litellm.proxy.retail.expression_pricing import (
    evaluate_expression,
    freeze_expression_context,
    validate_expression_policy,
)


@pytest.mark.parametrize("multiplier", ("1", "1.25", Decimal("2"), 3, 4.5))
def test_supported_modifier_keys_require_the_corresponding_provider(multiplier: object) -> None:
    anthropic: Final = {
        "litellm_provider": "anthropic",
        "provider_specific_entry": {"us": multiplier, "fast": multiplier},
    }
    openai: Final = {"litellm_provider": "openai", "regional_processing_uplift_multiplier_eu": multiplier}
    vertex: Final = {"litellm_provider": "vertex_ai-language-models", "regional_endpoint_uplift_multiplier": multiplier}
    assert supported_pricing_metadata(anthropic) == frozenset(("provider_specific_entry",))
    assert supported_pricing_metadata(openai) == frozenset(("regional_processing_uplift_multiplier_eu",))
    assert supported_pricing_metadata(vertex) == frozenset(("regional_endpoint_uplift_multiplier",))
    assert not supported_pricing_metadata({**anthropic, "litellm_provider": "other"})
    assert not supported_pricing_metadata({**openai, "litellm_provider": "other"})
    assert not supported_pricing_metadata({**vertex, "litellm_provider": "other"})


@pytest.mark.parametrize("invalid", (None, True, False, "NaN", "Infinity", "-1", "0", "0.5", "101", {}, "x", "9" * 200))
def test_invalid_multipliers_never_become_supported_pricing_dimensions(invalid: object) -> None:
    assert not supported_pricing_metadata({"litellm_provider": "anthropic", "provider_specific_entry": {"us": invalid}})
    assert not supported_pricing_metadata(
        {"litellm_provider": "openai", "regional_processing_uplift_multiplier_us": invalid}
    )
    assert not supported_pricing_metadata(
        {"litellm_provider": "vertex_ai", "regional_endpoint_uplift_multiplier": invalid}
    )


def test_provider_specific_unknown_dimensions_cannot_be_dropped_alongside_known_modifiers() -> None:
    assert not supported_pricing_metadata(
        {
            "litellm_provider": "anthropic",
            "provider_specific_entry": {"us": "1.25", "future_fee": "2"},
        }
    )
    assert supported_pricing_metadata(
        {"provider_specific_entry": {"bedrock_invocation_schema": "fixture"}}
    ) == frozenset(("provider_specific_entry",))


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai", "vertex_ai-language-models"))
@pytest.mark.parametrize("period", ("day", "month"))
def test_shared_allowance_is_explicitly_not_allocated_by_per_request_pricing(provider: str, period: str) -> None:
    metadata: Final = {
        "litellm_provider": provider,
        "search_free_requests": {"count": 17, "period": period, "shared_group": "fixture-group"},
    }
    assert supported_pricing_metadata(metadata) == frozenset(("search_free_requests",))
    assert "not passed through" in (catalog_pricing_note(metadata) or "")
    assert apply_token_modifiers("p * 2", metadata) == "p * 2"


@pytest.mark.parametrize(
    "allowance",
    (
        {"count": True, "period": "day", "shared_group": "fixture"},
        {"count": 0, "period": "day", "shared_group": "fixture"},
        {"count": "17", "period": "day", "shared_group": "fixture"},
        {"count": 17, "period": "year", "shared_group": "fixture"},
        {"count": 17, "period": "month", "shared_group": ""},
        {"count": 17, "period": "month", "shared_group": "unsafe\nvalue"},
        {"count": 17, "period": "month", "shared_group": "fixture", "price": "2"},
        {"count": 17, "period": "month"},
    ),
)
def test_malformed_shared_allowances_keep_review_required(allowance: dict[str, object]) -> None:
    metadata: Final = {"litellm_provider": "gemini", "search_free_requests": allowance}
    assert not supported_pricing_metadata(metadata)
    assert catalog_pricing_note(metadata) is None


def test_shared_allowances_for_unrecognized_providers_remain_unsupported() -> None:
    metadata: Final = {
        "litellm_provider": "openai",
        "search_free_requests": {"count": 17, "period": "day", "shared_group": "fixture"},
    }
    assert not supported_pricing_metadata(metadata)


@pytest.mark.parametrize(
    ("geo", "speed", "expected_multiplier"),
    (
        ("global", "standard", "1"),
        ("us", "standard", "1.25"),
        ("global", "fast", "3"),
        ("us", "fast", "3.75"),
    ),
)
def test_token_modifiers_stack_without_applying_to_separate_tool_fees(
    geo: str,
    speed: str,
    expected_multiplier: str,
) -> None:
    metadata: Final = {"litellm_provider": "anthropic", "provider_specific_entry": {"us": "1.25", "fast": "3"}}
    token_expression: Final = "p * 2 + c * 4 + cr * 0.2 + cc * 2.5 + cc1h * 4"
    modified: Final = apply_token_modifiers(token_expression, metadata)
    policy: Final = validate_expression_policy(
        {
            "expression": f'{modified} + fixed(u("web_search_requests") * 0.02)',
            "usage_bounds": {"web_search_requests": "8"},
        }
    )
    context: Final = freeze_expression_context(policy, {}, datetime(2026, 1, 1, tzinfo=timezone.utc))
    result: Final = evaluate_expression(
        policy,
        {
            "p": 100,
            "c": 50,
            "cr": 100,
            "cc": 100,
            "cc1h": 100,
            "attributes": {"inference_geo": geo, "speed": speed},
            "usage": {"web_search_requests": "2"},
        },
        context,
    )
    base: Final = (
        Decimal(100) * 2
        + Decimal(50) * 4
        + Decimal(100) * Decimal("0.2")
        + Decimal(100) * Decimal("2.5")
        + Decimal(100) * 4
    ) / 1_000_000
    assert Decimal(str(result["amount"])) == base * Decimal(expected_multiplier) + Decimal("0.04")
    assert ("inference geo: us" in result["tiers"]) == (geo == "us")
    assert ("speed: fast" in result["tiers"]) == (speed == "fast")


def test_missing_authoritative_modifier_attribute_does_not_guess_standard_pricing() -> None:
    expression: Final = apply_token_modifiers(
        "p * 2",
        {
            "litellm_provider": "anthropic",
            "provider_specific_entry": {"us": "1.25"},
        },
    )
    policy: Final = validate_expression_policy({"expression": expression})
    context: Final = freeze_expression_context(policy, {}, datetime(2026, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="Missing.*inference_geo"):
        evaluate_expression(policy, {"p": 100}, context)
