from decimal import Decimal

from litellm.proxy.retail.billing import reservation_id
from litellm.proxy.retail.pricing import UsageFacts, charge_for_usage, reserve_quote, usage_facts

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
