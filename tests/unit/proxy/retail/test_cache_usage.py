from typing import Final

import pytest

from litellm.proxy.retail.cache_usage import CacheUsage, cache_usage
from litellm.proxy.retail.pricing import UsageFacts, usage_facts


def test_anthropic_duration_aliases_are_counted_once_and_residual_stays_unclassified() -> None:
    facts: Final = cache_usage(
        {
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 30},
            "prompt_tokens_details": {
                "cached_tokens": 7,
                "cache_write_tokens": 100,
                "cache_creation_tokens": 100,
                "cache_creation_token_details": {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 30},
            },
        }
    )
    assert facts.model_dump() == {
        "cache_read_tokens": 7,
        "cache_write_tokens": 100,
        "cache_write_ttl_tokens": {"300": 20, "3600": 30},
        "cache_write_unclassified_tokens": 50,
    }


def test_explicit_duration_counts_supply_a_missing_or_zero_total_but_never_override_a_positive_total() -> None:
    split: Final = {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 30}
    inferred_total: Final = cache_usage({"cache_creation": split})
    assert inferred_total == CacheUsage(cache_write_tokens=50, cache_write_ttl_tokens={"300": 20, "3600": 30})
    with pytest.raises(ValueError, match="exceed"):
        cache_usage({"cache_creation_input_tokens": 20, "cache_creation": split})
    assert cache_usage({"cache_creation_input_tokens": 0, "cache_creation": split}) == inferred_total


def test_openai_mixed_ttls_remain_distinct_and_missing_ttl_is_not_guessed() -> None:
    mixed: Final = cache_usage(
        {
            "input_tokens_details": {
                "cache_write_tokens": 9,
                "cache_write_tokens_details": {"ttl_30m_tokens": 3, "ttl_12h_tokens": 6},
            }
        }
    )
    assert mixed.cache_write_ttl_tokens == {"1800": 3, "43200": 6}
    assert mixed.cache_write_unclassified_tokens == 0
    unknown: Final = cache_usage({"input_tokens_details": {"cache_write_tokens": 9}})
    assert unknown.cache_write_ttl_tokens == {}
    assert unknown.cache_write_unclassified_tokens == 9


@pytest.mark.parametrize("value", (-1, True, "2", 1.2))
def test_invalid_counts_never_become_billable_zero(value: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        cache_usage({"input_tokens_details": {"cache_write_tokens": value}})


def test_unrecognized_but_explicit_duration_is_preserved_for_custom_pricing() -> None:
    facts: Final = cache_usage(
        {"input_tokens_details": {"cache_write_tokens_details": {"ttl_90m_tokens": 8}}}
    )
    assert facts.cache_write_ttl_tokens == {"5400": 8}
    assert facts.cache_write_tokens == 8


@pytest.mark.parametrize("fields", (
    {"prompt_cache_hit_tokens": 20},
    {"cached_tokens": 20},
    {"input_details": {"cached_tokens": 20}},
    {"input_tokens_details": {"cached_tokens": 20}},
    {"cache_read_tokens": 20},
    {"prompt_tokens_details": {"cache_hit_tokens": 20}},
))
def test_compatible_cache_read_aliases_reduce_the_same_aggregate_input_once(fields: dict[str, object]) -> None:
    result: Final = usage_facts({"usage": {"prompt_tokens": 100, "completion_tokens": 10, **fields}})
    assert result == UsageFacts(input_tokens=80, output_tokens=10, cache_read_tokens=20)


@pytest.mark.parametrize("extra", (
    {"choices": [{"usage": {"cached_tokens": 20}}]},
    {"choices": [{"provider_specific_fields": {"usage": {"cached_tokens": 20}}}]},
    {"choices": [{}, {"usage": {"cached_tokens": 20}}]},
    {"timings": {"cache_n": 20}},
    {"provider_specific_fields": {"timings": {"cache_n": 20}}},
))
def test_moonshot_and_llamacpp_response_fields_supply_missing_canonical_cache_usage(extra: dict[str, object]) -> None:
    result: Final = usage_facts({"usage": {"prompt_tokens": 100, "completion_tokens": 10}, **extra})
    assert result == UsageFacts(input_tokens=80, output_tokens=10, cache_read_tokens=20)


def test_partial_detail_objects_and_mirrored_fields_do_not_hide_or_add_cache_tokens() -> None:
    result: Final = usage_facts({"usage": {
        "prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "input_tokens_details": {"cached_tokens": 20, "cached_creation_tokens": 30},
        "cache_creation_tokens": 30, "cache_write_tokens": 30,
    }})
    assert result == UsageFacts(input_tokens=50, output_tokens=10, cache_read_tokens=20, cache_write_tokens=30)


def test_a_positive_canonical_read_count_wins_over_legacy_alias_fallbacks() -> None:
    result: Final = usage_facts({"usage": {
        "prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 10},
    }})
    assert result == UsageFacts(input_tokens=90, output_tokens=10, cache_read_tokens=10)


def test_claude_partial_duration_usage_keeps_reported_input_and_both_cache_amounts() -> None:
    result: Final = usage_facts({"usage": {
        "input_tokens": 80, "output_tokens": 10, "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 30, "cache_creation": {"ephemeral_1h_input_tokens": 10},
    }})
    assert result == UsageFacts(
        input_tokens=80, output_tokens=10, cache_read_tokens=20, cache_write_tokens=30,
        cache_write_ttl_tokens={"3600": 10},
    )
    assert result.cache_write_unclassified_tokens == 20
