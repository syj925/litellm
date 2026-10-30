from datetime import datetime, timezone
from decimal import Decimal
from typing import Final

import pytest

from litellm.proxy.retail.catalog_expression import catalog_expression_policy
from litellm.proxy.retail.expression_pricing import (
    evaluate_expression,
    freeze_expression_context,
    validate_expression_policy,
)
from litellm.proxy.retail.pricing import catalog_candidates, catalog_snapshot

_METADATA: Final = {
    "mode": "chat",
    "litellm_provider": "openai",
    "input_cost_per_token": "0.000002",
    "output_cost_per_token": "0.000010",
    "cache_read_input_token_cost": "0.0000002",
    "cache_creation_input_token_cost": "0.0000025",
    "cache_creation_input_token_cost_above_12hr": "0.000005",
    "input_cost_per_token_above_100_tokens": "0.000004",
    "output_cost_per_token_above_100_tokens": "0.000020",
    "cache_read_input_token_cost_above_100_tokens": "0.0000004",
    "input_cost_per_token_priority": "0.000006",
    "input_cost_per_token_above_100_tokens_priority": "0.000008",
}


@pytest.mark.parametrize(
    ("context_length", "service", "input_rate", "output_rate", "read_rate"),
    (
        (100, "default", "2", "10", "0.2"),
        (101, "default", "4", "20", "0.4"),
        (100, "priority", "6", "10", "0.2"),
        (101, "priority", "8", "20", "0.4"),
    ),
)
def test_loaded_catalog_context_and_service_rules_use_full_input_boundary(
    context_length: int,
    service: str,
    input_rate: str,
    output_rate: str,
    read_rate: str,
) -> None:
    policy: Final = validate_expression_policy(catalog_expression_policy("fixture", _METADATA))
    context: Final = freeze_expression_context(
        policy, {"service_tier": service}, datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    result: Final = evaluate_expression(
        policy,
        {
            "p": 10,
            "c": 5,
            "len": context_length,
            "cr": context_length - 10,
            "cc": 0,
            "cc12h": 0,
        },
        context,
    )
    expected: Final = (
        Decimal(input_rate) * 10 + Decimal(output_rate) * 5 + Decimal(read_rate) * (context_length - 10)
    ) / 1000000
    assert Decimal(str(result["amount"])) == expected
    assert service in result["tiers"][0]


def test_catalog_candidates_preserve_all_deployment_consensus_for_expression_pricing() -> None:
    deployments: Final = ({"litellm_params": {"model": "openai/fixture"}},)
    catalog: Final = {"fixture": _METADATA}
    cards: Final = catalog_candidates("public-model", deployments, catalog, "Catalog fixture")
    snapshot: Final = catalog_snapshot("public-model", deployments, "1", catalog)
    assert cards[0]["expression_policy"] == snapshot["expression_policy"]
    assert snapshot["model"] == "public-model"
    with pytest.raises(ValueError, match="single supported"):
        catalog_snapshot("public-model", (*deployments, {"litellm_params": {"model": "missing"}}), "1", catalog)


def test_unknown_cost_dimensions_are_not_silently_dropped_from_generated_rules() -> None:
    assert catalog_expression_policy("fixture", {**_METADATA, "output_cost_per_pixel": "0.1"}) is None
    assert catalog_expression_policy("fixture", {**_METADATA, "input_cost_per_token_below_100_tokens": "0.1"}) is None


@pytest.mark.parametrize("extra", (
    {"off_peak_pricing": {"input_cost_per_token": "0.000001", "windows": [{"hours_utc": "00:00-06:00"}]}},
    {"tiered_pricing": [{"input_cost_per_token": "0.000001", "range": [0, 100]}]},
    {"regional_endpoint_uplift_multiplier": "1.1"},
    {"regional_processing_uplift_multiplier_eu": "1.1"},
    {"regional_processing_uplift_multiplier_us": "1.1"},
    {"provider_specific_entry": {"us": "1.1"}},
    {"provider_specific_entry": {"input_cost_per_token": "0.000001"}},
    {"search_free_requests": {"count": 100, "period": "day", "shared_group": "fixture"}},
    {"future_pricing": {"per_request": "0.1"}},
    {"per_request_fee": "0.1"},
    {"currency": "EUR"},
    {"currency": {}},
    {"web_search_billing_unit": "per_day"},
))
@pytest.mark.parametrize("tiered", (False, True))
def test_non_cost_pricing_dimensions_cannot_bypass_review_through_flat_or_expression_paths(
    extra: dict[str, object], tiered: bool,
) -> None:
    base: Final = _METADATA if tiered else {
        "mode": "chat", "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010",
    }
    metadata: Final = {**base, **extra}
    deployments: Final = ({"litellm_params": {"model": "openai/fixture"}},)
    assert catalog_expression_policy("fixture", metadata) is None
    assert catalog_candidates("public-model", deployments, {"fixture": metadata}) == ()
    with pytest.raises(ValueError, match="single supported"):
        catalog_snapshot("public-model", deployments, "1", {"fixture": metadata})


def test_nonpricing_provider_schema_and_capability_metadata_preserve_flat_reference_prices() -> None:
    metadata: Final = {
        "mode": "embedding", "input_cost_per_token": "0.000002", "output_cost_per_token": "0",
        "provider_specific_entry": {"bedrock_invocation_schema": "fixture_schema"},
        "supports_prompt_caching": True, "prompt_cache_min_tokens": 1024,
        "metadata": {"notes": "Published base pricing"},
        "off_peak_pricing": None, "regional_endpoint_uplift_multiplier": None,
    }
    card: Final = catalog_snapshot(
        "public-model", ({"litellm_params": {"model": "openai/fixture"}},), "1", {"fixture": metadata},
    )
    assert Decimal(str(card["input_cost_per_token"])) == Decimal("0.000002")
    assert "expression_policy" not in card
    assert catalog_expression_policy("fixture", metadata) is not None


def test_loaded_per_image_rates_remain_usd_per_returned_image() -> None:
    policy: Final = validate_expression_policy(
        catalog_expression_policy(
            "fixture-image",
            {"mode": "image_generation", "output_cost_per_image": "0.04"},
        )
    )
    context: Final = freeze_expression_context(policy, {"n": 5}, datetime(2026, 1, 1, tzinfo=timezone.utc))
    result: Final = evaluate_expression(policy, {"usage": {"images": "2"}}, context)
    assert Decimal(str(result["amount"])) == Decimal("0.08")


def test_plain_rates_keep_the_legacy_catalog_snapshot_path() -> None:
    rates: Final = {
        key: value for key, value in _METADATA.items() if "above_100_tokens" not in key and "priority" not in key
    }
    card: Final = catalog_snapshot(
        "public-model", ({"litellm_params": {"model": "openai/fixture"}},), "1", {"fixture": rates}
    )
    assert "expression_policy" not in card


def test_explicit_custom_cache_durations_survive_context_and_service_conversion_including_zero() -> None:
    policy: Final = validate_expression_policy(
        catalog_expression_policy(
            "fixture",
            {
                **_METADATA,
                "cache_write_tiers": {"750": "0", "43200": "0"},
            },
        )
    )
    context: Final = freeze_expression_context(policy, {}, datetime(2026, 1, 1, tzinfo=timezone.utc))
    result: Final = evaluate_expression(
        policy,
        {
            "p": 0,
            "c": 0,
            "len": 150,
            "cr": 0,
            "cc": 1,
            "cc12h": 100,
            "cache_write_ttl_tokens": {"750": 49},
        },
        context,
    )
    assert Decimal(str(result["amount"])) == Decimal("0.0000025")
    assert result["cache_write_ttl_tokens"] == {"750": "49"}


def test_service_suffix_before_context_uses_the_same_explicit_rate() -> None:
    policy: Final = validate_expression_policy(
        catalog_expression_policy(
            "fixture",
            {
                **_METADATA,
                "input_cost_per_token_priority_above_200_tokens": "0.000009",
            },
        )
    )
    context: Final = freeze_expression_context(
        policy, {"service_tier": "priority"}, datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    result: Final = evaluate_expression(policy, {"p": 100, "c": 0, "len": 201, "cr": 0, "cc": 0, "cc12h": 0}, context)
    assert Decimal(str(result["amount"])) == Decimal("0.0009")
