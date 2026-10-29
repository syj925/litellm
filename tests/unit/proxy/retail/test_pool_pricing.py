from decimal import Decimal
from typing import Final

import pytest

from litellm.proxy.retail.pool_pricing import _PoolFactors, pool_discount_view


@pytest.mark.parametrize(
    ("customer_model", "plan_model", "customer_default", "plan_default", "expected", "source"),
    (
        ("0.6", "0.7", "0.8", "0.9", "0.6", "customer_pool_model"),
        ("1", "0.7", "0.8", "0.9", "1", "customer_pool_model"),
        (None, "0.7", "0.8", "0.9", "0.7", "plan_pool_model"),
        (None, None, "0.8", "0.9", "0.8", "customer_pool_default"),
        (None, None, None, "0.9", "0.9", "plan_pool_default"),
        (None, None, None, None, "0.95", "pool_default"),
    ),
)
def test_pool_pricing_uses_one_scoped_discount_without_multiplying_other_levels(
    customer_model: str | None,
    plan_model: str | None,
    customer_default: str | None,
    plan_default: str | None,
    expected: str,
    source: str,
) -> None:
    factors: Final = _PoolFactors(
        user_id="customer",
        plan_id="shared",
        pool_id="bedrock",
        pool_name="Pool",
        pool_default_discount=Decimal("0.95"),
        customer_discount=Decimal(customer_model) if customer_model is not None else None,
        plan_discount=Decimal(plan_model) if plan_model is not None else None,
        customer_pool_discount=Decimal(customer_default) if customer_default is not None else None,
        plan_pool_discount=Decimal(plan_default) if plan_default is not None else None,
    )
    result: Final = pool_discount_view("public-model", factors)
    assert result["discount"] == expected
    assert result["discount_source"] == source
    assert result["pool_id"] == "bedrock"
    assert result["plan_id"] == "shared"
    assert result["model"] == "public-model"


def test_different_resource_classes_keep_their_own_pricing_scope_for_the_same_public_model() -> None:
    base: Final = _PoolFactors(
        user_id="customer",
        plan_id="shared",
        pool_id="official",
        pool_name="Official",
        pool_default_discount=Decimal("1"),
        customer_discount=Decimal("0.8"),
        plan_discount=None,
        customer_pool_discount=None,
        plan_pool_discount=None,
    )
    subscription: Final = base.model_copy(update={"pool_id": "subscription", "customer_discount": Decimal("0.4")})
    assert pool_discount_view("same-model", base)["discount"] == "0.8"
    assert pool_discount_view("same-model", subscription)["discount"] == "0.4"
    assert (
        pool_discount_view("same-model", base)["pool_id"] != pool_discount_view("same-model", subscription)["pool_id"]
    )
