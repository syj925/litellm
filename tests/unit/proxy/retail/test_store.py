from decimal import Decimal

import pytest

from litellm.proxy.retail.models import InvalidAmount, amount_to_nanos, cost_to_nanos, nanos_to_amount


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
