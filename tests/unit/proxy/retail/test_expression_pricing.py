from datetime import datetime, timezone
from decimal import Context, Decimal, localcontext
from typing import Final

import pytest

from litellm.proxy.retail.expression_pricing import (
    ExpressionPolicy,
    ExpressionPricingError,
    evaluate_expression,
    expression_cache_ttls,
    expression_usage_keys,
    expression_variables,
    freeze_expression_context,
    reserve_expression,
    validate_expression_policy,
)

_START: Final = datetime(2026, 11, 1, 3, 45, tzinfo=timezone.utc)


def _policy(expression: str, **fields: object) -> ExpressionPolicy:
    return validate_expression_policy({"version": "expression-v1", "expression": expression, **fields})


def test_token_coefficients_and_fixed_usage_have_different_units():
    policy: Final = _policy("p * 2 + c * 8 + fixed(u('images') * 0.04)", usage_bounds={"images": "10"})
    context: Final = freeze_expression_context(policy, {}, _START)
    result: Final = evaluate_expression(policy, {"p": 1_000_000, "c": 500_000, "usage": {"images": "3"}}, context)
    assert Decimal(result["amount"]) == Decimal("6.12")
    assert result["variables"] == {"p": "1000000", "c": "500000"}
    assert result["usage"] == {"images": "3"}
    assert expression_usage_keys(policy) == frozenset(("images",))


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("fixed(2+3*4)", "14"),
        ("fixed((2+3)*4)", "20"),
        ("fixed(20/2/2)", "5"),
        ("fixed(10-2-3)", "5"),
        ("0 ? fixed(1) : 1 ? fixed(2) : fixed(3)", "2"),
        ("fixed(abs(-2)+min(5,3)+max(1,4)+ceil(1.1)+floor(1.9))", "12"),
    ],
)
def test_operator_precedence_associativity_and_numeric_functions(expression: str, expected: str):
    policy: Final = _policy(expression)
    result: Final = evaluate_expression(policy, {}, freeze_expression_context(policy, {}, _START))
    assert result["amount"] == expected


def test_policy_bounds_are_deeply_frozen_and_serialize_as_json_object():
    policy: Final = _policy("u('requests')", usage_bounds={"requests": "1"})
    assert policy.model_dump(mode="json")["usage_bounds"] == {"requests": "1"}
    assert policy.usage_bounds["requests"] == "1"
    with pytest.raises(TypeError):
        policy.usage_bounds["requests"] = "2"


def test_extreme_decimal_exponents_cannot_expand_to_large_serialized_traces():
    with pytest.raises(ExpressionPricingError):
        _policy("u('requests')", usage_bounds={"requests": "1e-10000000"})
    policy: Final = _policy("p")
    context: Final = freeze_expression_context(policy, {}, _START)
    with pytest.raises(ExpressionPricingError):
        evaluate_expression(policy, {"p": "1e-10000000"}, context)
    zero: Final = evaluate_expression(policy, {"p": "0e-10000000"}, context)
    assert zero["amount"] == "0"
    assert zero["variables"] == {"p": "0"}


def test_branches_and_logical_operators_read_only_selected_facts():
    policy: Final = _policy("!p || p >= 100 ? tier('small', fixed(0.01)) : tier('large', c * 20)")
    context: Final = freeze_expression_context(policy, {}, _START)
    result: Final = evaluate_expression(policy, {"p": 0}, context)
    assert result["amount"] == "0.01"
    assert result["tiers"] == ["small"]
    assert result["variables"] == {"p": "0"}
    assert expression_variables(policy) == frozenset(("p", "c"))
    with pytest.raises(ExpressionPricingError, match="Missing usage variable 'c'"):
        evaluate_expression(policy, {"p": 50}, context)


@pytest.mark.parametrize(
    "expression, expected",
    [("0 && (1/0)", "0"), ("1 || (1/0)", "0.000001"), ("1 ? fixed(2) : 1/0", "2"), ("0 ? p : fixed(3)", "3")],
)
def test_short_circuit_skips_invalid_unused_operands(expression: str, expected: str):
    policy: Final = _policy(expression)
    assert evaluate_expression(policy, {}, freeze_expression_context(policy, {}, _START))["amount"] == expected


def test_complete_context_len_controls_tier_independent_of_cache_variables():
    policy: Final = _policy("len > 128000 ? tier('long',p*4+cr*0.4) : tier('short',p*2+cr*0.2)")
    result: Final = evaluate_expression(
        policy, {"len": 150000, "p": 1000, "cr": 149000}, freeze_expression_context(policy, {}, _START)
    )
    assert result["tiers"] == ["long"]
    assert Decimal(result["amount"]) == Decimal("0.0636")


def test_timezone_calendar_and_date_come_from_frozen_request_start():
    policy: Final = _policy(
        "weekday()==0 && hour()==11 && minute()==45 && month()==11 && day()==1"
        " && date()=='2026-11-01' && weekday('America/New_York')==6"
        " && date('America/New_York')=='2026-10-31' && time('hour','UTC')==3"
        " ? fixed(2.5) : fixed(8)",
        timezone="Asia/Shanghai",
    )
    context: Final = freeze_expression_context(policy, {}, _START)
    assert context["started_at"] == _START.isoformat()
    assert evaluate_expression(policy, {}, context)["amount"] == "2.5"


def test_frozen_params_copy_only_needed_scalars_and_defaults():
    policy: Final = _policy(
        "param('service_tier')=='default' && param('quality','standard')=='high' ? fixed(param('n')*0.04) : fixed(0.01)"
    )
    request: Final = {"quality": "high", "messages": [{"content": "private"}], "api_key": "private"}
    context: Final = freeze_expression_context(policy, request, _START)
    assert context["params"] == {"n": 1, "quality": "high", "service_tier": "default"}
    assert evaluate_expression(policy, {}, context)["amount"] == "0.04"
    assert evaluate_expression(policy, {}, context)["variables"] == {}
    assert "private" not in str(context)


def test_missing_optional_param_uses_expression_default_and_nested_path_is_safe():
    policy: Final = _policy("param('reasoning.effort','medium')=='high' ? fixed(2) : fixed(1)")
    assert evaluate_expression(policy, {}, freeze_expression_context(policy, {}, _START))["amount"] == "1"
    context: Final = freeze_expression_context(policy, {"reasoning": {"effort": "high", "text": "secret"}}, _START)
    assert context["params"] == {"reasoning.effort": "high"}
    assert evaluate_expression(policy, {}, context)["amount"] == "2"


@pytest.mark.parametrize(
    "expression",
    [
        "unknown+1",
        "open('x')",
        "__import__('os')",
        "p.__class__",
        "p[0]",
        "[p for p in c]",
        "p**2",
        "1e3",
        "p; fixed(1)",
        "param('messages')",
        "param('headers.authorization')",
        "param('api_key')",
        "param(p)",
        "u(p)",
        "tier(p,1)",
        "time('second')",
        "hour(p)",
        "fixed()",
        "min()",
        "p ? 1",
    ],
)
def test_policy_rejects_unknown_or_unsafe_syntax(expression: str):
    with pytest.raises(ExpressionPricingError):
        _policy(expression)


@pytest.mark.parametrize(
    "fields",
    [
        {"timezone": "Not/A_Zone"},
        {"usage_bounds": {"images": "NaN"}},
        {"usage_bounds": {"images": "-1"}},
        {"version": "expression-v2"},
    ],
)
def test_policy_rejects_invalid_configuration(fields: dict[str, object]):
    with pytest.raises(ExpressionPricingError):
        _policy("p", **fields)


def test_usage_functions_require_explicit_reservation_bound_not_live_facts():
    with pytest.raises(ExpressionPricingError, match="Missing usage bounds"):
        _policy("fixed(u('images')*0.04)")
    policy: Final = _policy("fixed(u('images')*0.04)", usage_bounds={"images": "5"})
    context: Final = freeze_expression_context(policy, {}, _START)
    with pytest.raises(ExpressionPricingError, match="Missing usage fact"):
        evaluate_expression(policy, {}, context)
    with pytest.raises(ExpressionPricingError, match="exceeds its configured bound"):
        evaluate_expression(policy, {"usage": {"images": "6"}}, context)
    assert reserve_expression(policy, context, 0, 0) == Decimal("0.2")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", True, {}, "not numeric"])
def test_numeric_facts_are_strictly_finite_and_nonnegative(value: object):
    policy: Final = _policy("p")
    with pytest.raises(ExpressionPricingError):
        evaluate_expression(policy, {"p": value}, freeze_expression_context(policy, {}, _START))


@pytest.mark.parametrize("expression", ["1/0", "fixed(-1)", "fixed(1000001)", "'text'", "param('size')"])
def test_invalid_result_and_missing_param_fail_closed(expression: str):
    policy: Final = _policy(expression)
    with pytest.raises(ExpressionPricingError):
        evaluate_expression(policy, {}, freeze_expression_context(policy, {}, _START))


@pytest.mark.parametrize(
    "expression", ["(" * 70 + "p" + ")" * 70, "p+" * 1500 + "p", " " * 16385, "'" + "x" * 257 + "'"]
)
def test_source_and_recursion_bounds_reject_expensive_input(expression: str):
    with pytest.raises(ExpressionPricingError):
        _policy(expression)


def test_request_context_rejects_naive_time_and_nonscalar_param():
    policy: Final = _policy("param('quality')=='high' ? p : c")
    with pytest.raises(ExpressionPricingError, match="timezone"):
        freeze_expression_context(policy, {}, _START.replace(tzinfo=None))
    with pytest.raises(ExpressionPricingError, match="scalar"):
        freeze_expression_context(policy, {"quality": {"text": "private"}}, _START)


def test_reservation_checks_every_branch_and_does_not_multiply_request_quantity_twice():
    policy: Final = _policy("p > 0 ? p * 2 + c * 8 : fixed(param('n')*0.04)")
    context: Final = freeze_expression_context(policy, {"n": 5}, _START)
    assert reserve_expression(policy, context, 1000, 1000) == Decimal("0.2")
    assert evaluate_expression(policy, {"p": 0}, context)["amount"] == "0.20"


@pytest.mark.parametrize(
    "expression",
    [
        "p*2+c*8",
        "p>5 ? tier('a', p*3+c*8) : tier('b',p*20+c)",
        "max(p*2,c*8)-min(p,c)",
        "abs(p-c)*3",
        "ceil(p/3)+floor(c/2)",
        "p/(c+1)",
    ],
)
def test_reservation_is_an_upper_bound_for_all_facts_in_input_output_ranges(expression: str):
    policy: Final = _policy(expression)
    context: Final = freeze_expression_context(policy, {}, _START)
    reservation: Final = reserve_expression(policy, context, 10, 7)
    for prompt in range(11):
        for completion in range(8):
            actual: Final = evaluate_expression(policy, {"p": prompt, "c": completion}, context)
            assert Decimal(actual["amount"]) <= reservation


@pytest.mark.parametrize("expression", ["1/p", "1/(p-5)", "1 ? fixed(1) : 1/p", "fixed(1000001)"])
def test_reservation_rejects_unbounded_or_over_limit_expressions(expression: str):
    policy: Final = _policy(expression)
    with pytest.raises(ExpressionPricingError):
        reserve_expression(policy, freeze_expression_context(policy, {}, _START), 10, 10)


def test_decimal_result_does_not_depend_on_callers_decimal_context():
    policy: Final = _policy("p * 0.123456789 + fixed(0.000000001)")
    context: Final = freeze_expression_context(policy, {}, _START)
    with localcontext(Context(prec=3)):
        actual: Final = evaluate_expression(policy, {"p": 123456}, context)
        bound: Final = reserve_expression(policy, context, 123456, 0)
    assert Decimal(actual["amount"]) == Decimal("0.015241482342784")
    assert bound == Decimal(actual["amount"])


def test_custom_cache_write_ttl_uses_token_units_and_input_reservation_bound():
    policy: Final = _policy("cache_write('750')*3+cc*2")
    context: Final = freeze_expression_context(policy, {}, _START)
    actual: Final = evaluate_expression(policy, {"cache_write_ttl_tokens": {"750": 40}, "cc": 10}, context)
    assert actual["amount"] == "0.00014"
    assert actual["cache_write_ttl_tokens"] == {"750": "40"}
    assert expression_cache_ttls(policy) == frozenset(("750",))
    assert reserve_expression(policy, context, 100, 0) == Decimal("0.0005")


@pytest.mark.parametrize("ttl", ["0", "-1", "0750", "750.0", "1e3", "31536001", "9" * 100, "../750"])
def test_cache_write_ttl_requires_bounded_canonical_integer_seconds(ttl: str):
    with pytest.raises(ExpressionPricingError):
        _policy(f"cache_write('{ttl}')")


@pytest.mark.parametrize(
    "expression",
    [
        "cache_write(750)",
        "cache_write(p)",
        "cache_write('750',p)",
        "cc1h+cache_write('3600')",
        "cc5m+cache_write('300')",
    ],
)
def test_cache_write_ttl_rejects_dynamic_arguments_and_overlapping_named_aliases(expression: str):
    with pytest.raises(ExpressionPricingError):
        _policy(expression)


def test_missing_custom_cache_ttl_facts_obey_lazy_branch_evaluation():
    policy: Final = _policy("1?fixed(1):cache_write('750')")
    assert evaluate_expression(policy, {}, freeze_expression_context(policy, {}, _START))["amount"] == "1"
    missing: Final = _policy("cache_write('750')")
    with pytest.raises(ExpressionPricingError, match="Missing cache write TTL usage"):
        evaluate_expression(missing, {}, freeze_expression_context(missing, {}, _START))


def test_reservation_rejects_an_always_negative_final_amount():
    policy: Final = _policy("fixed(-1)")
    with pytest.raises(ExpressionPricingError, match="between 0 and 1000000"):
        reserve_expression(policy, freeze_expression_context(policy, {}, _START), 0, 0)


def test_reported_predicate_does_not_require_or_price_missing_variables():
    policy: Final = _policy("reported('img_o') ? img_o*4 : fixed(0.08)")
    context: Final = freeze_expression_context(policy, {}, _START)
    assert evaluate_expression(policy, {}, context)["amount"] == "0.08"
    assert evaluate_expression(policy, {"img_o": 100}, context)["amount"] == "0.0004"
    assert reserve_expression(policy, context, 0, 100) == Decimal("0.08")
    presence_only: Final = _policy("reported('img_o') ? fixed(2) : fixed(1)")
    assert expression_variables(presence_only) == frozenset()
    assert evaluate_expression(presence_only, {}, freeze_expression_context(presence_only, {}, _START))["amount"] == "1"


def test_reported_usage_presence_can_check_quantities_without_reservation_bounds():
    policy: Final = _policy("reported('usage.images') ? fixed(2) : fixed(1)")
    context: Final = freeze_expression_context(policy, {}, _START)
    assert expression_usage_keys(policy) == frozenset()
    assert evaluate_expression(policy, {}, context)["amount"] == "1"
    assert evaluate_expression(policy, {"usage": {"images": 0}}, context)["amount"] == "2"
    with pytest.raises(ExpressionPricingError, match="must be numeric"):
        evaluate_expression(policy, {"usage": {"images": "not numeric"}}, context)


def test_reported_tokens_marker_is_available_only_as_a_presence_predicate():
    policy: Final = _policy("reported('tokens') ? fixed(2) : fixed(1)")
    context: Final = freeze_expression_context(policy, {}, _START)
    assert evaluate_expression(policy, {"tokens": 100}, context)["amount"] == "2"
    assert evaluate_expression(policy, {}, context)["amount"] == "1"
    assert expression_variables(policy) == frozenset()
    with pytest.raises(ExpressionPricingError, match="Unknown variable"):
        _policy("tokens")


@pytest.mark.parametrize(
    "expression",
    [
        "reported(p)",
        "reported('messages')",
        "reported('headers.authorization')",
        "reported('p.__class__')",
        "reported('usage.')",
        "reported('usage.a[0]')",
    ],
)
def test_reported_predicate_rejects_dynamic_or_unsafe_paths(expression: str):
    with pytest.raises(ExpressionPricingError):
        _policy(expression)


def test_response_attributes_select_actual_tier_and_record_only_consumed_public_fields():
    policy: Final = _policy(
        "attr('service_tier',param('service_tier'))=='fast' ? tier('fast',p*6) : tier('standard',p*2)"
    )
    context: Final = freeze_expression_context(policy, {"service_tier": "fast", "api_key": "private"}, _START)
    downgraded: Final = evaluate_expression(
        policy, {"p": 100, "attributes": {"service_tier": "default", "api_key": "private"}}, context,
    )
    assert Decimal(str(downgraded["amount"])) == Decimal("0.0002")
    assert downgraded["tiers"] == ["standard"]
    assert downgraded["variables"] == {"attr.service_tier": "default", "p": "100"}
    assert "private" not in str(downgraded)
    fallback: Final = evaluate_expression(policy, {"p": 100}, context)
    assert Decimal(str(fallback["amount"])) == Decimal("0.0006")
    assert context["params"] == {"service_tier": "fast"}


def test_missing_response_attributes_fail_only_when_referenced_and_reserve_all_possible_rates():
    policy: Final = _policy("attr('inference_geo')=='us' ? p*4 : p*2")
    context: Final = freeze_expression_context(policy, {}, _START)
    reservation: Final = reserve_expression(policy, context, 100, 0)
    assert reservation == Decimal("0.0004")
    for geo in ("us", "global"):
        actual: Final = evaluate_expression(policy, {"p": 100, "attributes": {"inference_geo": geo}}, context)
        assert Decimal(str(actual["amount"])) <= reservation
    with pytest.raises(ExpressionPricingError, match="Missing response attribute 'inference_geo'"):
        evaluate_expression(policy, {"p": 100}, context)
    lazy: Final = _policy("1 ? p : attr('inference_geo')=='us' ? p*4 : p*2")
    assert evaluate_expression(lazy, {"p": 100}, freeze_expression_context(lazy, {}, _START))["amount"] == "0.0001"


@pytest.mark.parametrize("value", ["", "x" * 257, "us\nprivate", 1, True, {}])
def test_response_attribute_values_must_be_bounded_printable_strings(value: object):
    policy: Final = _policy("attr('speed')=='fast' ? p*4 : p*2")
    with pytest.raises(ExpressionPricingError, match="Response attribute"):
        evaluate_expression(
            policy, {"p": 100, "attributes": {"speed": value}}, freeze_expression_context(policy, {}, _START),
        )


@pytest.mark.parametrize("expression", ["attr('api_key')", "attr('headers')", "attr(p)", "attr('usage.inference_geo')"])
def test_response_attribute_paths_cannot_access_arbitrary_response_or_usage_fields(expression: str):
    with pytest.raises(ExpressionPricingError):
        _policy(expression)


def test_inference_geo_and_speed_request_fallbacks_freeze_safe_scalars():
    policy: Final = _policy("param('inference_geo')=='us' && param('speed')=='fast' ? p*4 : p*2")
    context: Final = freeze_expression_context(
        policy, {"inference_geo": "us", "speed": "fast", "messages": "private"}, _START,
    )
    assert context["params"] == {"inference_geo": "us", "speed": "fast"}
    assert evaluate_expression(policy, {"p": 100}, context)["amount"] == "0.0004"


@pytest.mark.parametrize(
    "path, value",
    [
        ("inference_geo", "not_available"),
        ("inference_geo", "eu"),
        ("speed", "turbo"),
        ("data_residency", "unresolved"),
        ("service_tier", "unknown"),
        ("vertex_location", "https://private.example"),
        ("vertex_location", "x" * 64),
    ],
)
def test_unknown_pricing_attributes_cannot_silently_choose_the_base_branch(path: str, value: str):
    policy: Final = _policy(f"attr('{path}')=='us' ? fixed(2) : fixed(1)")
    with pytest.raises(ExpressionPricingError, match="attribute"):
        evaluate_expression(policy, {"attributes": {path: value}}, freeze_expression_context(policy, {}, _START))


def test_deployment_vertex_location_is_normalized_and_available_to_comparisons():
    policy: Final = _policy("attr('vertex_location')=='europe-west4' ? fixed(2) : fixed(1)")
    actual: Final = evaluate_expression(
        policy, {"attributes": {"vertex_location": "EUROPE-WEST4"}}, freeze_expression_context(policy, {}, _START),
    )
    assert actual["amount"] == "2"
    assert actual["variables"] == {"attr.vertex_location": "europe-west4"}
