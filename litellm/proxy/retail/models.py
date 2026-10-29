from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

NANOS_PER_USD: Final = Decimal(1_000_000_000)
MAX_AMOUNT_NANOS: Final = 9_000_000_000_000_000_000
_MAX_USD: Final = Decimal(MAX_AMOUNT_NANOS) / NANOS_PER_USD
_USD_PRECISION: Final = Decimal("0.000000001")


class WalletError(Exception):
    pass


class WalletNotFound(WalletError):
    pass


class ReservationNotFound(WalletError):
    pass


class InsufficientFunds(WalletError):
    pass


class ParallelLimitExceeded(WalletError):
    pass


class IdempotencyConflict(WalletError):
    pass


class InvalidAmount(WalletError):
    pass


class PlanNotFound(WalletError):
    pass


def amount_to_nanos(amount: str | Decimal, *, allow_zero: bool = True) -> int:
    try:
        value: Final = Decimal(amount)
        if not value.is_finite() or value < 0 or (not allow_zero and value == 0):
            raise InvalidAmount("Amount must be a positive, finite USD value")
        if value > _MAX_USD or value.quantize(_USD_PRECISION) != value:
            raise InvalidAmount("Amount must have at most nine decimal places and fit the wallet limit")
        return int(value * NANOS_PER_USD)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InvalidAmount("Amount must be a decimal USD string") from exc


def cost_to_nanos(cost: str | Decimal) -> int:
    try:
        value: Final = Decimal(cost)
        if not value.is_finite() or value < 0 or value > _MAX_USD:
            raise InvalidAmount("Cost must be a finite, non-negative USD value within the wallet limit")
        return int(value.quantize(_USD_PRECISION, rounding=ROUND_CEILING) * NANOS_PER_USD)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InvalidAmount("Cost must be a decimal USD string") from exc


def nanos_to_amount(nanos: int) -> str:
    return format(Decimal(nanos) / NANOS_PER_USD, ".9f")


class PricePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    multiplier: str
    discount: str


class WalletView(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    currency: str = "USD"
    balance: str
    reserved: str
    available: str
    total_credited: str
    total_spent: str
    pricing_plan: PricePlan
    discount: str
    discount_override: str | None
    discount_source: Literal["customer", "plan"]
    pending_requests: int


def parse_discount(value: str) -> Decimal:
    try:
        discount: Final = Decimal(value)
        if not discount.is_finite() or not 0 < discount <= 1 or discount.as_tuple().exponent < -9:
            raise InvalidAmount("Discount must be greater than zero and at most 1, with up to nine decimal places")
        return discount
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidAmount("Discount must be a decimal value between zero and 1") from exc


class ReservationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    user_id: str
    model: str
    reserved_amount: str
    amount: str | None
    status: str
    price_snapshot: dict[str, JsonValue]
    details: dict[str, JsonValue]
    created_at: str
    updated_at: str
