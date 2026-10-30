from decimal import Decimal
from typing import Final

import pytest
from pydantic import JsonValue

from litellm.proxy.retail.models import InvalidAmount, amount_to_nanos, cost_to_nanos, nanos_to_amount
from litellm.proxy.retail.store import (
    _model_expression_policy,
    _model_price_published,
    _wallet_view,
    _WalletRow,
    model_discount_view,
)


@pytest.mark.parametrize("amount", ("0", "0.000000001", "0.125", "12.345678901", "1000000"))
def test_wallet_amount_round_trip_preserves_every_supported_decimal(amount: str) -> None:
    assert Decimal(nanos_to_amount(amount_to_nanos(amount))) == Decimal(amount)


@pytest.mark.parametrize(
    "amount",
    (
        "-1",
        "NaN",
        "Infinity",
        "-Infinity",
        "0.0000000001",
        "9000000001",
        "1e10000000",
        "1e-10000000",
        "1.000000000000000000000000000000000001",
    ),
)
def test_wallet_credit_rejects_invalid_or_unrepresentable_values(amount: str) -> None:
    with pytest.raises(InvalidAmount):
        amount_to_nanos(amount)


def test_wallet_does_not_discard_a_fractional_nano_of_provider_cost() -> None:
    assert cost_to_nanos("0.00000000001") == 1
    assert cost_to_nanos("0.00000000101") == 2
    assert cost_to_nanos("1.000000000000000000000000000000000001") == 1_000_000_001


def test_manual_credit_cannot_silently_become_a_zero_amount() -> None:
    with pytest.raises(InvalidAmount):
        amount_to_nanos("0", allow_zero=False)


def test_negative_balance_remains_visible_as_debt() -> None:
    assert nanos_to_amount(-1250000000) == "-1.250000000"


def test_customer_override_replaces_the_plan_discount_and_clearing_it_restores_the_plan() -> None:
    row: Final = _WalletRow(
        user_id="customer",
        balance_nanos=1000000000,
        reserved_nanos=0,
        total_credited_nanos=1000000000,
        total_spent_nanos=0,
        plan_id="shared",
        plan_name="Shared",
        multiplier=Decimal("0.8"),
        discount_override=Decimal("0.65"),
        pending_requests=0,
    )
    overridden: Final = _wallet_view(row)
    inherited: Final = _wallet_view(row.model_copy(update={"discount_override": None}))
    assert overridden["discount"] == "0.65"
    assert overridden["discount_source"] == "customer"
    assert overridden["pricing_plan"]["discount"] == "0.8"
    assert inherited["discount"] == "0.8"
    assert inherited["discount_source"] == "plan"
    assert inherited["discount_override"] is None
    assert overridden["balance"] == inherited["balance"] == "1.000000000"


def test_legacy_markup_is_readable_without_being_clamped_or_rewritten() -> None:
    row: Final = _WalletRow(
        user_id="legacy",
        balance_nanos=1000000000,
        reserved_nanos=0,
        total_credited_nanos=1000000000,
        total_spent_nanos=0,
        plan_id="legacy",
        plan_name="Legacy",
        multiplier=Decimal("1.25"),
        pending_requests=0,
    )
    view: Final = _wallet_view(row)
    assert view["discount"] == "1.25"
    assert view["pricing_plan"]["multiplier"] == "1.25"
    assert view["discount_source"] == "plan"


@pytest.mark.parametrize(
    ("customer", "plan", "legacy_customer", "legacy_plan", "expected", "source"),
    (
        ("0.7", "0.8", None, "1", "0.7", "customer_model"),
        ("1", "0.8", None, "1", "1", "customer_model"),
        (None, "0.8", "0.5", "0.9", "0.8", "plan_model"),
        (None, None, None, "1", "1", "standard"),
        (None, None, "0.65", "0.8", "0.65", "legacy_customer"),
        (None, None, "1", "0.8", "1", "standard"),
        (None, None, None, "1.25", "1.25", "legacy_plan"),
    ),
)
def test_only_one_model_discount_applies_and_legacy_fallbacks_are_preserved(
    customer: str | None,
    plan: str | None,
    legacy_customer: str | None,
    legacy_plan: str,
    expected: str,
    source: str,
) -> None:
    row: Final = model_discount_view(
        "public-model",
        customer_discount=Decimal(customer) if customer is not None else None,
        plan_discount=Decimal(plan) if plan is not None else None,
        legacy_customer=Decimal(legacy_customer) if legacy_customer is not None else None,
        legacy_plan=Decimal(legacy_plan),
    )
    assert row == {
        "model": "public-model",
        "discount": expected,
        "discount_source": source,
        "customer_discount": customer,
        "plan_discount": plan,
    }


def test_removing_one_model_override_inherits_its_plan_without_changing_another_model() -> None:
    before: Final = model_discount_view("model-a", customer_discount=Decimal("0.6"), plan_discount=Decimal("0.8"))
    after: Final = model_discount_view("model-a", plan_discount=Decimal("0.8"))
    other: Final = model_discount_view("model-b", customer_discount=Decimal("0.7"), plan_discount=Decimal("0.9"))
    assert before["discount"] == "0.6"
    assert after["discount"] == "0.8"
    assert after["discount_source"] == "plan_model"
    assert other["discount"] == "0.7"
    assert model_discount_view("not-negotiated")["discount"] == "1"


@pytest.mark.parametrize("value", (None, False, True))
def test_publication_provenance_keeps_explicit_flags_and_preserves_legacy_manual_defaults(value: bool | None) -> None:
    assert _model_price_published({"cache_write_price_published": value}, "catalog") is value
    assert _model_price_published({"cache_write_price_published": value}, "manual") is (True if value is None else value)
    assert _model_price_published({}, "manual") is True


@pytest.mark.parametrize("value", ("false", 0, [], {}))
@pytest.mark.parametrize("source", ("catalog", "manual"))
def test_publication_provenance_does_not_coerce_unknown_values_into_price_evidence(value: JsonValue, source: str) -> None:
    with pytest.raises(InvalidAmount, match="Published cache write"):
        _model_price_published({"cache_write_price_published": value}, source)


def test_missing_and_null_policies_clear_expression_pricing_while_valid_policies_are_normalized() -> None:
    assert _model_expression_policy({}) is None
    assert _model_expression_policy({"expression_policy": None}) is None
    assert _model_expression_policy({"expression_policy": {
        "version": "expression-v1", "expression": "p * 2 + c * 6",
    }}) == {
        "version": "expression-v1", "expression": "p * 2 + c * 6", "timezone": "UTC", "usage_bounds": {},
    }


@pytest.mark.parametrize("policy", ("p * 2", {"expression": "__import__('os')"}, {"expression": "p + secret"}))
def test_storage_rejects_invalid_expression_policies_before_opening_a_transaction(policy: JsonValue) -> None:
    with pytest.raises(InvalidAmount):
        _model_expression_policy({"expression_policy": policy})
