from datetime import datetime, timezone
from decimal import Decimal
from typing import Final

import pytest

from litellm.proxy.retail.billing import price_snapshot, reservation_id
from litellm.proxy.retail.pricing import (
    UsageFacts,
    catalog_cache_write_tiers,
    catalog_candidates,
    catalog_snapshot,
    charge_for_usage,
    cost_breakdown,
    reserve_quote,
    usage_facts,
    validate_requested_cache_ttls,
    validated_cache_write_tiers,
)
from litellm.types.llms.openai import ResponseAPIUsage
from litellm.types.utils import Usage

SNAPSHOT = {
    "input_cost_per_token": "0.000002",
    "output_cost_per_token": "0.000010",
    "cache_read_input_token_cost": "0.0000002",
    "cache_creation_input_token_cost": "0.0000025",
    "multiplier": "1.5",
}


def test_openai_and_anthropic_cache_usage_produce_same_customer_charge():
    openai = usage_facts(
        {
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 100, "cache_creation_tokens": 5},
            }
        }
    )
    anthropic = usage_facts(
        {
            "usage": {
                "input_tokens": 15,
                "output_tokens": 10,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 5,
            }
        }
    )
    assert (
        openai
        == anthropic
        == UsageFacts(input_tokens=15, output_tokens=10, cache_read_tokens=100, cache_write_tokens=5)
    )
    assert charge_for_usage(SNAPSHOT, openai) == "0.000243750"


def test_responses_cached_input_is_not_charged_twice():
    facts = usage_facts(
        {"usage": {"input_tokens": 105, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 100}}}
    )
    assert facts == UsageFacts(input_tokens=5, output_tokens=10, cache_read_tokens=100)


def test_unknown_usage_stays_pending_instead_of_becoming_a_free_request():
    assert usage_facts({"choices": []}) is None
    assert usage_facts({"usage": {}}) is None
    assert usage_facts({"usage": {"prompt_tokens": None, "completion_tokens": None}}) is None
    assert usage_facts({"usage": {"prompt_tokens": 10}}) is None
    assert usage_facts({"usage": {"prompt_tokens": -1, "completion_tokens": 1}}) is None
    assert (
        usage_facts(
            {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 2}}}
        )
        is None
    )
    assert usage_facts({"usage": {"prompt_tokens": 10}}, allow_input_only=True) == UsageFacts(
        input_tokens=10, output_tokens=0
    )


def test_fractional_nano_charge_rounds_up_once_and_quote_covers_same_facts():
    snapshot = {**SNAPSHOT, "input_cost_per_token": "0.00000000001", "multiplier": "1"}
    assert charge_for_usage(snapshot, UsageFacts(input_tokens=1, output_tokens=0)) == "0.000000001"
    quote = reserve_quote(SNAPSHOT, {"messages": [{"role": "user", "content": "hello"}]}, 32)
    charge = charge_for_usage(SNAPSHOT, UsageFacts(input_tokens=20, output_tokens=32))
    assert Decimal(quote) >= Decimal(charge)


def test_logging_metadata_preserves_server_reservation_across_stream_callbacks():
    assert (
        reservation_id({"litellm_params": {"litellm_metadata": {"retail_request_id": "server-reservation"}}})
        == "server-reservation"
    )


def test_customer_discount_is_the_final_factor_and_legacy_snapshots_keep_the_original_multiplier() -> None:
    facts: Final = UsageFacts(input_tokens=10, output_tokens=2, cache_read_tokens=5, cache_write_tokens=3)
    snapshot: Final = {**SNAPSHOT, "multiplier": "0.8", "discount": "0.65"}
    assert charge_for_usage(snapshot, facts) == "0.000031525"
    assert charge_for_usage(SNAPSHOT, facts) == "0.000072750"
    assert snapshot["multiplier"] == "0.8"


def test_catalog_reference_ignores_all_configured_channel_cost_overrides() -> None:
    catalog: Final = {"model-a": {"input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010"}}
    deployments: Final = (
        {
            "litellm_params": {"model": "openai/model-a", "input_cost_per_token": 99, "output_cost_per_token": 999},
            "model_info": {"input_cost_per_token": 11, "output_cost_per_token": 111},
        },
    )
    card: Final = catalog_snapshot("public-model", deployments, "0.7", catalog)
    assert card["input_cost_per_token"] == "0.000002"
    assert card["output_cost_per_token"] == "0.000010"
    assert card["model"] == "public-model"
    assert card["catalog_model"] == "model-a"
    assert card["source"] == "catalog"
    assert card["multiplier"] == "0.7"


def test_multiple_channel_credentials_for_one_catalog_model_share_one_reference() -> None:
    catalog: Final = {"model-a": {"input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010"}}
    deployments: Final = (
        {"litellm_params": {"model": "openai/model-a", "api_base": "https://one.example"}},
        {"litellm_params": {"model": "openai/model-a", "api_base": "https://two.example"}},
    )
    assert len(catalog_candidates("public-model", deployments, catalog)) == 1
    assert catalog_snapshot("public-model", deployments, "1", catalog)["catalog_model"] == "model-a"


@pytest.mark.parametrize("second_model", ("model-b", "unknown-model"))
def test_ambiguous_or_unrecognized_deployments_require_an_explicit_reference_choice(second_model: str) -> None:
    catalog: Final = {
        "model-a": {"input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010"},
        "model-b": {"input_cost_per_token": "0.000001", "output_cost_per_token": "0.000020"},
    }
    deployments: Final = (
        {"litellm_params": {"model": "model-a"}},
        {"litellm_params": {"model": second_model}},
    )
    with pytest.raises(ValueError, match="single supported catalog reference"):
        catalog_snapshot("public-model", deployments, "1", catalog)


@pytest.mark.parametrize(
    "extra",
    (
        {"input_cost_per_pixel_above_200k_tokens": "0.000004"},
        {"cache_creation_input_token_cost_above_1hr": "0.000004"},
        {"mode": "unsupported_generation"},
        {"cache_read_input_token_cost": "NaN"},
        {"output_cost_per_token": None},
    ),
)
def test_unsupported_or_invalid_catalog_prices_cannot_silently_become_flat_or_free(extra: dict[str, object]) -> None:
    catalog: Final = {
        "model-a": {
            "input_cost_per_token": "0.000002",
            "output_cost_per_token": "0.000010",
            **extra,
        }
    }
    deployments: Final = ({"litellm_params": {"model": "model-a"}},)
    assert catalog_candidates("public-model", deployments, catalog) == ()
    with pytest.raises(ValueError, match="single supported catalog reference"):
        catalog_snapshot("public-model", deployments, "1", catalog)


def test_real_chat_and_responses_usage_types_keep_mixed_write_ttls_and_input_partition() -> None:
    details: Final = {
        "cached_tokens": 2,
        "cache_write_tokens": 4,
        "cache_write_tokens_details": {"ttl_30m_tokens": 1, "ttl_12h_tokens": 3},
    }
    response_usage: Final = ResponseAPIUsage(input_tokens=10, output_tokens=1, total_tokens=11, input_tokens_details=details)
    chat_usage: Final = Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11, prompt_tokens_details=details)
    expected: Final = UsageFacts(
        input_tokens=4, output_tokens=1, cache_read_tokens=2, cache_write_tokens=4,
        cache_write_ttl_tokens={"1800": 1, "43200": 3},
    )
    assert usage_facts({"usage": response_usage}) == expected
    assert usage_facts({"usage": chat_usage}) == expected


def test_duration_rates_discount_and_rounding_are_applied_once_with_reconstructable_details() -> None:
    snapshot: Final = {
        **SNAPSHOT, "version": "token-v2", "discount": "0.7",
        "cache_write_tiers": {"300": "0.000003", "3600": "0.000005"},
    }
    facts: Final = UsageFacts(
        input_tokens=10, output_tokens=2, cache_read_tokens=5, cache_write_tokens=9,
        cache_write_ttl_tokens={"300": 2, "3600": 3},
    )
    expected: Final = (Decimal("0.000002") * 10 + Decimal("0.000010") * 2 + Decimal("0.0000002") * 5
                       + Decimal("0.000003") * 2 + Decimal("0.000005") * 3 + Decimal("0.0000025") * 4) * Decimal("0.7")
    amount: Final = charge_for_usage(snapshot, facts)
    assert Decimal(amount) == expected
    breakdown: Final = cost_breakdown(snapshot, facts)
    assert breakdown["cache_write_tiers"] == {"300": "0.0000042", "3600": "0.0000105"}
    assert breakdown["cache_write_unclassified"] == "0.00000700"
    assert breakdown["rounding_adjustment"] == "0.000000000"
    assert Decimal(reserve_quote(snapshot, {"messages": []}, 2)) >= Decimal(amount)


def test_known_ttl_without_price_stays_unsettled_while_historical_flat_snapshots_keep_the_old_charge() -> None:
    facts: Final = UsageFacts(input_tokens=0, output_tokens=0, cache_write_tokens=4, cache_write_ttl_tokens={"43200": 4})
    with pytest.raises(ValueError, match="43200"):
        charge_for_usage({**SNAPSHOT, "version": "token-v2", "cache_write_tiers": {"1800": "0.000003"}}, facts)
    assert charge_for_usage({**SNAPSHOT, "version": "flat-token-v1"}, facts) == charge_for_usage(SNAPSHOT, facts)
    assert charge_for_usage(SNAPSHOT, facts) == "0.000015000"


def test_catalog_extracts_duration_prices_without_flattening_context_tiers() -> None:
    rates: Final = {
        "litellm_provider": "openai", "mode": "chat", "input_cost_per_token": "0.000002",
        "output_cost_per_token": "0.000010", "cache_creation_input_token_cost": "0.000003",
        "cache_creation_input_token_cost_above_12hr": "0.000006",
    }
    assert catalog_cache_write_tiers("openai/fixture", rates) == {"1800": "0.000003", "43200": "0.000006"}
    catalog: Final = {"openai/fixture": rates}
    deployments: Final = ({"litellm_params": {"model": "openai/fixture"}},)
    card: Final = catalog_snapshot("fixture", deployments, "1", catalog)
    assert card["cache_write_tiers"] == {"1800": "0.000003", "43200": "0.000006"}
    assert card["version"] == "token-v3"
    advanced: Final = {"openai/fixture": {**rates, "input_cost_per_token_above_272k_tokens": "0.000004"}}
    dynamic: Final = catalog_candidates("fixture", deployments, advanced)
    assert dynamic[0]["cache_write_tiers"] == card["cache_write_tiers"]
    assert "len > 272000" in dynamic[0]["expression_policy"]["expression"]
    assert "cc12h" in dynamic[0]["expression_policy"]["expression"]


def test_native_gemini_counts_include_cached_input_and_reasoning_without_guessing_storage() -> None:
    facts: Final = usage_facts({"usageMetadata": {
        "promptTokenCount": 100, "toolUsePromptTokenCount": 2, "cachedContentTokenCount": 30,
        "candidatesTokenCount": 4, "thoughtsTokenCount": 6,
    }})
    assert facts == UsageFacts(input_tokens=72, output_tokens=10, cache_read_tokens=30)
    assert facts.cache_write_ttl_tokens == {}


@pytest.mark.parametrize("tiers", ({"0": "1"}, {"01": "1"}, {"300": "NaN"}, {"300": "-1"}, {"300": "0.0000000000000000001"}))
def test_invalid_duration_prices_cannot_be_saved_or_quoted(tiers: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="Cache write"):
        validated_cache_write_tiers(tiers)


def test_explicit_unpriced_cache_ttl_is_rejected_before_provider_access() -> None:
    snapshot: Final = {**SNAPSHOT, "version": "token-v2", "cache_write_tiers": {"1800": "0.000003"}}
    with pytest.raises(ValueError, match="43200"):
        validate_requested_cache_ttls(snapshot, {"input": [{"prompt_cache_breakpoint": {"ttl": "12h"}}]})
    validate_requested_cache_ttls(snapshot, {"prompt_cache_options": {"ttl": "30m"}})


@pytest.mark.parametrize("default_rate", ("0.0000025", "0"))
def test_new_snapshots_charge_unconfigured_durations_at_the_frozen_default_without_losing_ttl(default_rate: str) -> None:
    snapshot: Final = {
        **SNAPSHOT, "version": "token-v3", "discount": "0.65",
        "cache_creation_input_token_cost": default_rate, "cache_write_tiers": {"300": "0.000004"},
    }
    facts: Final = UsageFacts(
        input_tokens=2, output_tokens=1, cache_read_tokens=3, cache_write_tokens=10,
        cache_write_ttl_tokens={"300": 2, "43200": 5},
    )
    expected: Final = (
        Decimal("0.000002") * 2 + Decimal("0.000010") + Decimal("0.0000002") * 3
        + Decimal("0.000004") * 2 + Decimal(default_rate) * 8
    ) * Decimal("0.65")
    amount: Final = charge_for_usage(snapshot, facts)
    details: Final = cost_breakdown(snapshot, facts)
    assert Decimal(amount) == expected
    assert details["cache_write_tiers"] == {
        "300": format(Decimal("0.000004") * 2 * Decimal("0.65"), "f"),
        "43200": format(Decimal(default_rate) * 5 * Decimal("0.65"), "f"),
    }
    assert Decimal(str(details["cache_write_unclassified"])) == Decimal(default_rate) * 3 * Decimal("0.65")
    assert sum((Decimal(str(details[key])) for key in ("input", "output", "cache_read", "cache_write_unclassified")),
               sum((Decimal(value) for value in details["cache_write_tiers"].values()), Decimal(0))) == expected
    assert facts.cache_write_ttl_tokens == {"300": 2, "43200": 5}
    assert Decimal(reserve_quote(snapshot, {"messages": []}, 1)) >= Decimal(amount)
    validate_requested_cache_ttls(snapshot, {"input": [{"prompt_cache_breakpoint": {"ttl": "12h"}}]})


def test_new_snapshots_use_default_write_pricing_without_requiring_duration_rows() -> None:
    snapshot: Final = {**SNAPSHOT, "version": "token-v3", "cache_write_tiers": {}}
    known: Final = UsageFacts(input_tokens=0, output_tokens=0, cache_write_tokens=4,
                             cache_write_ttl_tokens={"3600": 4})
    aggregate: Final = UsageFacts(input_tokens=0, output_tokens=0, cache_write_tokens=4)
    assert charge_for_usage(snapshot, known) == charge_for_usage(snapshot, aggregate) == "0.000015000"
    assert cost_breakdown(snapshot, known)["cache_write_tiers"] == {"3600": "0.00001500"}
    assert cost_breakdown(snapshot, aggregate)["cache_write_unclassified"] == "0.00001500"


def test_an_explicit_free_duration_wins_over_a_paid_default_write_price() -> None:
    snapshot: Final = {**SNAPSHOT, "version": "token-v3", "cache_write_tiers": {"3600": "0"}}
    facts: Final = UsageFacts(input_tokens=0, output_tokens=0, cache_write_tokens=4,
                             cache_write_ttl_tokens={"3600": 4})
    assert charge_for_usage(snapshot, facts) == "0.000000000"
    assert Decimal(cost_breakdown(snapshot, facts)["cache_write_tiers"]["3600"]) == 0


def test_new_requests_freeze_the_compatible_policy_and_the_original_price_version() -> None:
    snapshot: Final = price_snapshot(
        {**SNAPSHOT, "version": 7},
        {"discount": "0.65", "discount_source": "customer_model", "model": "fixture", "plan_id": "shared"},
        "aresponses",
    )
    assert snapshot["version"] == "token-v3"
    assert snapshot["price_version"] == 7
    assert snapshot["cache_creation_input_token_cost"] == SNAPSHOT["cache_creation_input_token_cost"]


@pytest.mark.parametrize("input_capacity", (128000, 2000000))
def test_opaque_expression_quote_uses_frozen_capacity_instead_of_legacy_floor_or_encoded_input_size(
    input_capacity: int,
) -> None:
    snapshot: Final = price_snapshot(
        {**SNAPSHOT, "model": "fixture", "version": 1,
         "expression_policy": {"version": "expression-v1", "expression": "p*2", "timezone": "UTC",
                               "usage_bounds": {"input_tokens": str(input_capacity), "output_tokens": "512"}}},
        {"model": "fixture", "plan_id": "shared", "discount": "1", "discount_source": "customer_model"},
        "aresponses", request_data={}, started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    short: Final = reserve_quote(snapshot, {"input": "small"}, 0, minimum_input_tokens=1000000)
    encoded: Final = reserve_quote(snapshot, {"input": "x" * (input_capacity + 1)}, 0, minimum_input_tokens=1000000)
    assert Decimal(short) == Decimal(encoded) == Decimal(input_capacity) * Decimal("0.000002")
    assert Decimal(reserve_quote(snapshot, {"input": "small"}, 0)) < Decimal(short)


def test_legacy_flat_quote_keeps_the_opaque_input_floor() -> None:
    quote: Final = reserve_quote(SNAPSHOT, {}, 0, minimum_input_tokens=1000000)
    same_maximum_rate_usage: Final = UsageFacts(input_tokens=0, output_tokens=0, cache_write_tokens=1000000)
    assert quote == charge_for_usage(SNAPSHOT, same_maximum_rate_usage)


@pytest.mark.parametrize("rate", (None, "NaN", "-1"))
def test_new_snapshots_cannot_silently_use_an_invalid_default_cache_write_price(rate: object) -> None:
    snapshot: Final = {**SNAPSHOT, "version": "token-v3", "cache_creation_input_token_cost": rate}
    with pytest.raises(ValueError, match="default cache write"):
        validate_requested_cache_ttls(snapshot, {"cache_control": {"ttl": "1h"}})
