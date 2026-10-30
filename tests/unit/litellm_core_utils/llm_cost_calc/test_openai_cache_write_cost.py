"""Cache-write pricing for OpenAI models, on both the chat and Responses paths.

Regression guard for https://github.com/BerriAI/litellm/issues/33772: OpenAI
reports cache-write tokens under ``prompt_tokens_details.cache_write_tokens``
(chat) / ``input_tokens_details.cache_write_tokens`` (responses), where
Anthropic reports ``cache_creation_tokens``. The cost path read only the
Anthropic name, so cache-write tokens were billed at the plain input rate,
the Responses transform dropped the split before cost ran, and the tiered
``cache_creation_input_token_cost_{priority,flex,above_272k_tokens}`` keys were
discarded by ``get_model_info``.
"""

from typing import Final

import pytest

import litellm
from litellm.litellm_core_utils.llm_cost_calc.utils import (
    calculate_cache_writing_cost,
    generic_cost_per_token,
    get_token_type_cost_breakdown,
)
from litellm.litellm_core_utils.streaming_handler import calculate_total_usage
from litellm.responses.litellm_completion_transformation.transformation import LiteLLMCompletionResponsesConfig
from litellm.responses.utils import ResponseAPILoggingUtils
from litellm.types.utils import CacheCreationTokenDetails, ModelInfo, ModelResponse, Usage

MODEL = "gpt-5.6"


def _openai_chat_usage(prompt_tokens: int, cache_write_tokens: int, completion_tokens: int) -> Usage:
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tokens_details={
            "cached_tokens": 0,
            "cache_write_tokens": cache_write_tokens,
        },
    )


def test_openai_cache_write_tokens_billed_at_the_cache_creation_rate(local_model_cost_map):
    """A cache-write request costs the cache-creation rate on the written tokens,
    not the plain input rate."""
    rates = litellm.model_cost[MODEL]
    input_rate = rates["input_cost_per_token"]
    cache_write_rate = rates["cache_creation_input_token_cost"]
    output_rate = rates["output_cost_per_token"]
    assert cache_write_rate > input_rate

    prompt_tokens = 12317
    cache_write_tokens = 12314
    fresh_tokens = prompt_tokens - cache_write_tokens
    completion_tokens = 5

    prompt_cost, completion_cost = generic_cost_per_token(
        model=MODEL,
        usage=_openai_chat_usage(prompt_tokens, cache_write_tokens, completion_tokens),
        custom_llm_provider="openai",
    )

    assert prompt_cost == pytest.approx(fresh_tokens * input_rate + cache_write_tokens * cache_write_rate)
    assert completion_cost == pytest.approx(completion_tokens * output_rate)
    assert prompt_cost > prompt_tokens * input_rate


def test_responses_api_cache_write_costs_the_same_as_chat(local_model_cost_map):
    """The Responses usage transform carries the cache-write split through, so the
    same request costs the same whichever route it took."""
    prompt_tokens = 12317
    cache_write_tokens = 12314
    completion_tokens = 5

    responses_usage = ResponseAPILoggingUtils.transform_response_api_usage_to_chat_usage(
        {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "input_tokens_details": {
                "cached_tokens": 0,
                "cache_write_tokens": cache_write_tokens,
            },
        }
    )
    assert responses_usage.prompt_tokens_details.cache_write_tokens == cache_write_tokens

    responses_prompt_cost, responses_completion_cost = generic_cost_per_token(
        model=MODEL,
        usage=responses_usage,
        custom_llm_provider="openai",
    )
    chat_prompt_cost, chat_completion_cost = generic_cost_per_token(
        model=MODEL,
        usage=_openai_chat_usage(prompt_tokens, cache_write_tokens, completion_tokens),
        custom_llm_provider="openai",
    )

    rates = litellm.model_cost[MODEL]
    fresh_tokens = prompt_tokens - cache_write_tokens
    assert responses_prompt_cost == pytest.approx(
        fresh_tokens * rates["input_cost_per_token"]
        + cache_write_tokens * rates["cache_creation_input_token_cost"]
    )
    assert responses_prompt_cost == pytest.approx(chat_prompt_cost)
    assert responses_completion_cost == pytest.approx(chat_completion_cost)


@pytest.mark.parametrize(
    "service_tier,prompt_tokens,rate_key",
    [
        (None, 100000, "cache_creation_input_token_cost"),
        ("priority", 100000, "cache_creation_input_token_cost_priority"),
        ("flex", 100000, "cache_creation_input_token_cost_flex"),
        (None, 300000, "cache_creation_input_token_cost_above_272k_tokens"),
    ],
)
def test_tiered_cache_creation_rates_are_registered_and_billed(
    local_model_cost_map, service_tier, prompt_tokens, rate_key
):
    """The tiered cache-creation keys survive ``get_model_info`` and are the rate the
    cost path actually charges for priority, flex, and >272k requests."""
    model_info = litellm.get_model_info(model=MODEL, custom_llm_provider="openai")
    tiered_rate = litellm.model_cost[MODEL][rate_key]
    assert model_info.get(rate_key) == tiered_rate

    cache_write_tokens = prompt_tokens - 1000
    fresh_tokens = 1000
    input_rate_key = {
        "cache_creation_input_token_cost": "input_cost_per_token",
        "cache_creation_input_token_cost_priority": "input_cost_per_token_priority",
        "cache_creation_input_token_cost_flex": "input_cost_per_token_flex",
        "cache_creation_input_token_cost_above_272k_tokens": "input_cost_per_token_above_272k_tokens",
    }[rate_key]
    input_rate = litellm.model_cost[MODEL][input_rate_key]

    prompt_cost, _ = generic_cost_per_token(
        model=MODEL,
        usage=_openai_chat_usage(prompt_tokens, cache_write_tokens, completion_tokens=100),
        custom_llm_provider="openai",
        service_tier=service_tier,
    )

    assert prompt_cost == pytest.approx(fresh_tokens * input_rate + cache_write_tokens * tiered_rate)


def _mixed_ttl_usage(prompt_tokens: int = 10) -> Usage:
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=1,
        total_tokens=prompt_tokens + 1,
        prompt_tokens_details={
            "cached_tokens": 2, "cache_write_tokens": 4,
            "cache_write_tokens_details": {"ttl_30m_tokens": 1, "ttl_12h_tokens": 3},
        },
    )


@pytest.mark.parametrize(
    ("service_tier", "prompt_tokens", "rate_suffix"),
    ((None, 10, ""), ("priority", 10, "_priority"),
     (None, 300000, "_above_272k_tokens"), ("priority", 300000, "_above_272k_tokens_priority")),
)
def test_openai_ttl_prices_use_the_same_context_and_service_tier_in_totals_and_breakdown(
    service_tier: str | None, prompt_tokens: int, rate_suffix: str
) -> None:
    model: Final = "fixture-cache-duration-model"
    rates: Final[ModelInfo] = {
        "key": model, "input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6,
        "cache_read_input_token_cost": 1e-7, "cache_creation_input_token_cost": 2e-6,
        "cache_creation_input_token_cost_above_12hr": 3e-6,
        "input_cost_per_token_priority": 4e-6, "output_cost_per_token_priority": 6e-6,
        "cache_read_input_token_cost_priority": 2e-7, "cache_creation_input_token_cost_priority": 5e-6,
        "cache_creation_input_token_cost_above_12hr_priority": 9e-6,
        "input_cost_per_token_above_272k_tokens": 7e-6, "output_cost_per_token_above_272k_tokens": 8e-6,
        "cache_read_input_token_cost_above_272k_tokens": 3e-7,
        "cache_creation_input_token_cost_above_272k_tokens": 4e-6,
        "cache_creation_input_token_cost_above_12hr_above_272k_tokens": 15e-6,
        "input_cost_per_token_above_272k_tokens_priority": 9e-6,
        "output_cost_per_token_above_272k_tokens_priority": 10e-6,
        "cache_read_input_token_cost_above_272k_tokens_priority": 4e-7,
        "cache_creation_input_token_cost_above_272k_tokens_priority": 6e-6,
        "cache_creation_input_token_cost_above_12hr_above_272k_tokens_priority": 21e-6,
    }
    usage: Final = _mixed_ttl_usage(prompt_tokens)
    prompt_cost, completion_cost = generic_cost_per_token(
        model=model, usage=usage, custom_llm_provider="openai", service_tier=service_tier, model_info=rates,
    )
    expected_write: Final = rates["cache_creation_input_token_cost" + rate_suffix] + 3 * rates[
        "cache_creation_input_token_cost_above_12hr" + rate_suffix
    ]
    assert prompt_cost == pytest.approx(
        (prompt_tokens - 6) * rates["input_cost_per_token" + rate_suffix]
        + 2 * rates["cache_read_input_token_cost" + rate_suffix] + expected_write
    )
    assert completion_cost == pytest.approx(rates["output_cost_per_token" + rate_suffix])
    breakdown: Final = get_token_type_cost_breakdown(
        model=model, usage=usage, custom_llm_provider="openai", service_tier=service_tier, model_info=rates,
    )
    assert breakdown.cache_creation_cost == pytest.approx(expected_write)
    assert breakdown.cache_write_costs_by_ttl == {
        "1800": rates["cache_creation_input_token_cost" + rate_suffix],
        "43200": 3 * rates["cache_creation_input_token_cost_above_12hr" + rate_suffix],
    }
    assert breakdown.cache_write_unclassified_cost == 0
    assert breakdown.rates.cache_creation_input_token_cost_above_12hr == rates[
        "cache_creation_input_token_cost_above_12hr" + rate_suffix
    ]


def test_unknown_ttl_price_cannot_silently_fall_back_to_the_short_write_price() -> None:
    with pytest.raises(ValueError, match="12-hour"):
        generic_cost_per_token(
            model="fixture-missing-duration-price", usage=_mixed_ttl_usage(), custom_llm_provider="openai",
            model_info={"key": "fixture-missing-duration-price", "input_cost_per_token": 1e-6,
                        "output_cost_per_token": 2e-6, "cache_creation_input_token_cost": 3e-6},
        )


@pytest.mark.parametrize("details", ({"ttl_30m_tokens": 1, "ttl_12h_tokens": 3},
                                      {"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 3}))
def test_ttl_details_survive_responses_roundtrip_and_a_later_stream_usage_without_the_split(
    details: dict[str, int]
) -> None:
    usage: Final = Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11, prompt_tokens_details={
        "cached_tokens": 2, "cache_write_tokens": 4,
        "cache_write_tokens_details" if "ttl_12h_tokens" in details else "cache_creation_token_details": details,
    })
    response: Final = LiteLLMCompletionResponsesConfig.transform_chat_completion_usage_to_responses_usage(usage)
    returned: Final = ResponseAPILoggingUtils.transform_response_api_usage_to_chat_usage(response)
    assert returned.prompt_tokens_details.cache_creation_token_details.model_dump(exclude_none=True) == details
    final: Final = calculate_total_usage([
        ModelResponse(usage=usage),
        ModelResponse(usage=Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12,
                                 prompt_tokens_details={"cached_tokens": 2, "cache_write_tokens": 4})),
    ])
    assert final.prompt_tokens_details.cache_creation_token_details.model_dump(exclude_none=True) == details
    assert final.prompt_tokens_details.cache_write_tokens == 4


def test_aggregate_and_duration_counts_are_not_billed_twice_and_partial_counts_keep_the_remainder() -> None:
    assert calculate_cache_writing_cost(10, CacheCreationTokenDetails(ttl_30m_tokens=2, ttl_12h_tokens=3),
                                        4e-6, 2e-6, 6e-6) == pytest.approx(7 * 2e-6 + 3 * 6e-6)
    with pytest.raises(ValueError, match="exceed"):
        calculate_cache_writing_cost(4, CacheCreationTokenDetails(ephemeral_5m_input_tokens=4,
                                                                ephemeral_1h_input_tokens=4), 4e-6, 2e-6)


def test_custom_flat_breakdown_preserves_the_explicit_flat_pricing_contract() -> None:
    rates: Final = {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6,
                    "cache_creation_input_token_cost": 3e-6}
    breakdown: Final = get_token_type_cost_breakdown(
        model="fixture-flat", usage=_mixed_ttl_usage(), custom_llm_provider="openai", custom_cost_per_token=rates,
    )
    assert breakdown.cache_creation_cost == pytest.approx(4 * rates["cache_creation_input_token_cost"])
    assert sum(breakdown.cache_write_costs_by_ttl.values()) == pytest.approx(breakdown.cache_creation_cost)


def test_get_model_info_preserves_registered_long_ttl_price_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    model: Final = "fixture-cache-duration-info"
    rates: Final = {
        "litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6,
        "cache_creation_input_token_cost_above_12hr": 3e-6,
        "cache_creation_input_token_cost_above_12hr_above_272k_tokens_priority": 8e-6,
    }
    monkeypatch.setitem(litellm.model_cost, model, rates)
    info: Final = litellm.get_model_info(model, custom_llm_provider="openai")
    assert info["cache_creation_input_token_cost_above_12hr"] == rates["cache_creation_input_token_cost_above_12hr"]
    assert info["cache_creation_input_token_cost_above_12hr_above_272k_tokens_priority"] == rates[
        "cache_creation_input_token_cost_above_12hr_above_272k_tokens_priority"
    ]
