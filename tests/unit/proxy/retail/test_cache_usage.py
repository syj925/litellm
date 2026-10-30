from typing import Final

import pytest

from litellm.proxy.retail.cache_usage import CacheUsage, cache_usage


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


def test_explicit_duration_counts_supply_a_missing_total_but_never_override_a_reported_total() -> None:
    split: Final = {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 30}
    inferred_total: Final = cache_usage({"cache_creation": split})
    assert inferred_total == CacheUsage(cache_write_tokens=50, cache_write_ttl_tokens={"300": 20, "3600": 30})
    with pytest.raises(ValueError, match="exceed"):
        cache_usage({"cache_creation_input_tokens": 20, "cache_creation": split})
    with pytest.raises(ValueError, match="exceed"):
        cache_usage({"cache_creation_input_tokens": 0, "cache_creation": split})


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
