from decimal import Decimal
from typing import Final

import pytest

from litellm.proxy.retail.billing import reservation_id
from litellm.proxy.retail.pricing import (
    UsageFacts,
    catalog_candidates,
    catalog_snapshot,
    charge_for_usage,
    reserve_quote,
    usage_facts,
)

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
        {"input_cost_per_token_above_200k_tokens": "0.000004"},
        {"cache_creation_input_token_cost_above_1hr": "0.000004"},
        {"mode": "image_generation"},
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
    with pytest.raises(ValueError):
        catalog_snapshot("public-model", deployments, "1", catalog)
