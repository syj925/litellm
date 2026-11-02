from datetime import datetime, timezone
from decimal import Decimal
from typing import Final

import pytest

from litellm.proxy.retail.catalog_expression import catalog_expression_policy
from litellm.proxy.retail.expression_display import build_expression_display
from litellm.proxy.retail.expression_pricing import (
    evaluate_expression,
    freeze_expression_context,
    validate_expression_policy,
)

_START: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _policy(expression: str, **bounds: str) -> dict[str, object]:
    return {"expression": expression, "usage_bounds": bounds, "timezone": "UTC"}


@pytest.mark.parametrize(("context", "service"), ((100, "default"), (101, "default"), (100, "priority"), (101, "fast")))
def test_service_context_rows_preserve_coherent_prices_and_match_the_settlement_engine(context: int, service: str):
    policy: Final = validate_expression_policy(
        catalog_expression_policy(
            "fixture",
            {
                "litellm_provider": "openai",
                "mode": "chat",
                "input_cost_per_token": "0.000002",
                "output_cost_per_token": "0.000008",
                "cache_read_input_token_cost": "0.0000002",
                "input_cost_per_token_above_100_tokens": "0.000004",
                "output_cost_per_token_above_100_tokens": "0.000016",
                "cache_read_input_token_cost_above_100_tokens": "0.0000004",
                "input_cost_per_token_priority": "0.000006",
                "output_cost_per_token_priority": "0.000024",
                "input_cost_per_token_above_100_tokens_priority": "0.000012",
                "output_cost_per_token_above_100_tokens_priority": "0.000048",
            },
        )
    )
    display: Final = build_expression_display(policy)
    assert display is not None and display.status == "complete"
    assert display.token_rates[0].baseline
    assert display.token_rates[0].conditions == ("Standard", "Input context ≤ 100 tokens")
    rows: Final = tuple(
        row
        for row in display.token_rates
        if (
            ("Standard" in row.conditions) == (service == "default")
            and any(
                ("> 100" in condition) == (context > 100)
                for condition in row.conditions
                if "Input context" in condition
            )
        )
    )
    assert len(rows) == 1
    facts: Final = {"p": 10, "c": 5, "cr": 2, "cc": 0, "len": context, "attributes": {"service_tier": service}}
    calculated: Final = evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))
    rendered: Final = sum((Decimal(rate) * Decimal(str(facts[key])) for key, rate in rows[0].rates.items()), Decimal(0))
    assert rendered / 1_000_000 == Decimal(str(calculated["amount"]))


@pytest.mark.parametrize(("geo", "speed"), (("global", "standard"), ("us", "standard"), ("us", "fast")))
def test_modifiers_apply_to_token_subtotal_and_do_not_multiply_search_fees(geo: str, speed: str):
    raw: Final = _policy(
        '(p*2+c*8) * (attr("inference_geo")=="us" ? tier("US",1.2) : 1) '
        '* (attr("speed")=="fast" ? 3 : 1) + fixed(min(1,u("web_search_requests"))*0.04)',
        web_search_requests="64",
    )
    display: Final = build_expression_display(raw)
    assert display is not None and display.status == "complete"
    assert len(display.token_rates) == 1
    assert display.token_rates[0].rates == {"p": "2", "c": "8"}
    assert {row.multiplier for row in display.modifiers} == {"1.2", "3"}
    assert all(row.applies_to == "Token charges" for row in display.modifiers)
    assert display.fees[0].amount == "0.04" and display.fees[0].unit == "grounded_request"
    policy: Final = validate_expression_policy(raw)
    result: Final = evaluate_expression(
        policy,
        {
            "p": 1000,
            "c": 500,
            "attributes": {"inference_geo": geo, "speed": speed},
            "usage": {"web_search_requests": 5},
        },
        freeze_expression_context(policy, {}, _START),
    )
    multiplier: Final = (Decimal("1.2") if geo == "us" else 1) * (3 if speed == "fast" else 1)
    assert Decimal(str(result["amount"])) == Decimal("0.006") * multiplier + Decimal(display.fees[0].amount)


def test_calendar_multiplier_retains_its_date_and_timezone_without_changing_the_base_price():
    raw: Final = _policy('(p*2+c*8)*(date("UTC")>="2026-12-01" ? 2 : 1)')
    display: Final = build_expression_display(raw)
    assert display is not None and display.status == "complete"
    assert display.modifiers[0].conditions == ("Request date (UTC) ≥ 2026-12-01",)
    assert display.modifiers[0].multiplier == "2"
    assert display.token_rates[0].rates == {"p": "2", "c": "8"}
    assert any("request start time" in note for note in display.notes)


def test_image_alternatives_remain_conditional_and_are_never_presented_as_additive_charges():
    raw: Final = _policy('reported("tokens") ? tier("tokens",p*2+img_o*16) : fixed(u("images")*0.05)', images="16")
    display: Final = build_expression_display(raw)
    assert display is not None and display.status == "complete"
    assert display.token_rates[0].conditions == ("Standard", "Token usage reported")
    assert not display.token_rates[0].baseline
    assert display.fees[0].conditions == ("No token usage reported",)
    assert display.fees[0].amount == "0.05" and display.fees[0].unit == "image"


@pytest.mark.parametrize(
    "expression", ('fixed(u("images")*0.05)', 'fixed(0.05)*u("images")', 'u("images")*fixed(0.05)')
)
def test_per_unit_fees_remain_usd_per_actual_reported_unit(expression: str):
    raw: Final = _policy(expression, images="16")
    display: Final = build_expression_display(raw)
    assert display is not None and display.status == "complete"
    assert display.token_rates == ()
    assert display.fees[0].usage_key == "images" and display.fees[0].unit == "image"
    policy: Final = validate_expression_policy(raw)
    result: Final = evaluate_expression(policy, {"usage": {"images": 3}}, freeze_expression_context(policy, {}, _START))
    assert Decimal(display.fees[0].amount) * 3 == Decimal(str(result["amount"]))


def test_custom_time_fees_have_readable_conditions_and_keep_both_alternatives():
    display: Final = build_expression_display(_policy("weekday()==0.0 || weekday()==6 ? fixed(0.04) : fixed(0.02)"))
    assert display is not None and display.status == "complete"
    assert len(display.fees) == 2
    assert "Sunday" in display.fees[0].conditions[0] and "Saturday" in display.fees[0].conditions[0]
    assert display.fees[1].conditions[0].startswith("Outside (")
    assert {row.amount for row in display.fees} == {"0.04", "0.02"}


def test_custom_cache_duration_and_duplicate_terms_keep_exact_effective_rates_including_zero():
    display: Final = build_expression_display(_policy('p*2+p*3+c*0+cache_write("750")*4'))
    assert display is not None and display.status == "complete"
    assert display.token_rates[0].rates == {"p": "5", "c": "0", "cache_write_750": "4"}


@pytest.mark.parametrize(
    "expression",
    (
        "p*c",
        "p/(c+1)",
        "p*2-c*3",
        "max(p*2,c*3)",
        "len>100 ? p*2 : p*3 + (len>200 ? c*5 : c*4)",
    ),
)
def test_unsupported_or_ambiguous_rules_never_expose_partial_numerical_prices(expression: str):
    display: Final = build_expression_display(_policy(expression))
    assert display is not None and display.status == "unsupported"
    assert not display.token_rates and not display.fees and not display.modifiers
    assert display.notes


def test_missing_and_invalid_policies_are_distinguished_without_exposing_a_made_up_price():
    assert build_expression_display(None) is None
    display: Final = build_expression_display({"expression": "broken()"})
    assert display is not None and display.status == "unsupported"
    assert display.token_rates == () and display.fees == ()


def test_explicit_standard_condition_does_not_present_its_else_price_as_standard():
    display: Final = build_expression_display(_policy('param("service_tier","default")=="default" ? p*2 : p*8'))
    assert display is not None and display.status == "complete"
    assert display.token_rates[0].baseline and display.token_rates[0].rates["p"] == "2"
    assert not display.token_rates[1].baseline
    assert display.token_rates[1].conditions == ("Other services (excluding Standard)",)
    assert not any("actual served tier" in note for note in display.notes)


def test_redundant_service_conditions_do_not_add_unreachable_or_mislabeled_prices():
    display: Final = build_expression_display(
        _policy('param("service_tier")=="flex" ? p*2 : param("service_tier")=="default" ? p*4 : p*8')
    )
    assert display is not None and display.status == "complete"
    assert len(tuple(row for row in display.token_rates if row.baseline)) == 1
    assert display.token_rates[0].rates["p"] == "4"
    assert any("Other services (excluding Standard, Flex)" in row.conditions for row in display.token_rates)
    unreachable: Final = build_expression_display(
        _policy('param("service_tier")=="flex" ? p*2 : param("service_tier")=="flex" ? p*999 : p*4')
    )
    assert unreachable is not None and all(row.rates["p"] != "999" for row in unreachable.token_rates)


def test_a_modifier_on_a_mixed_subtotal_is_not_misrepresented_as_covering_unrelated_fees():
    display: Final = build_expression_display(
        _policy('(p*2+fixed(0.04))*(attr("inference_geo")=="us" ? 2 : 1)+fixed(0.03)')
    )
    assert display is not None and display.status == "unsupported"
    assert not display.token_rates and not display.fees and not display.modifiers


def test_unknown_capped_units_remain_explicitly_unsupported_instead_of_losing_the_cap():
    display: Final = build_expression_display(_policy('fixed(min(1,u("widgets"))*3)', widgets="8"))
    assert display is not None and display.status == "unsupported" and display.fees == ()


def test_batch_prices_are_identified_as_reference_only_and_uncapped_search_is_provider_neutral():
    display: Final = build_expression_display(
        _policy(
            '(param("service_tier")=="batches" ? p*1 : p*2)+fixed(u("web_search_requests")*0.04)',
            web_search_requests="64",
        )
    )
    assert display is not None and display.status == "complete"
    assert any("Batch prices are reference only" in note for note in display.notes)
    assert display.fees[0].unit == "search"


def test_standard_and_service_groups_remain_contiguous_with_short_context_before_long_context():
    policy: Final = catalog_expression_policy(
        "fixture",
        {
            "litellm_provider": "gemini",
            "mode": "chat",
            "input_cost_per_token": "0.000002",
            "output_cost_per_token": "0.000008",
            "input_cost_per_token_above_100_tokens": "0.000004",
            "input_cost_per_token_flex": "0.000001",
            "input_cost_per_token_priority": "0.000006",
            "input_cost_per_token_batches": "0.0000005",
        },
    )
    display: Final = build_expression_display(policy)
    assert display is not None and display.status == "complete"
    assert tuple(row.conditions[0] for row in display.token_rates) == (
        "Standard",
        "Standard",
        "Service: Priority",
        "Service: Priority",
        "Service: Flex",
        "Service: Flex",
        "Service: Batch",
        "Service: Batch",
    )
    assert all("Input context ≤ 100 tokens" in display.token_rates[index].conditions for index in (0, 2, 4, 6))
    assert all("Input context > 100 tokens" in display.token_rates[index].conditions for index in (1, 3, 5, 7))
