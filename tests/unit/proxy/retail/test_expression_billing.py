from datetime import datetime, timezone
from decimal import Decimal
from typing import Final

import pytest

from litellm.proxy.retail.billing import price_snapshot
from litellm.proxy.retail.expression_billing import catalog_routing_attributes, expression_settlement
from litellm.proxy.retail.pricing import reserve_quote

_DISCOUNT: Final = {"model": "fixture", "discount": "0.7", "discount_source": "customer_model", "plan_id": "standard"}
_BASIS: Final = {
    "model": "fixture",
    "version": 9,
    "input_cost_per_token": "0.000002",
    "output_cost_per_token": "0.000008",
    "cache_read_input_token_cost": "0.0000002",
    "cache_creation_input_token_cost": "0.0000025",
}


def test_verified_deployment_region_is_frozen_and_cannot_be_spoofed_by_response_or_request() -> None:
    deployment: Final = {"litellm_params": {"model": "openai/fixture", "api_base": "https://eu.api.openai.com/v1"}}
    attributes: Final = catalog_routing_attributes((deployment,))
    assert attributes == {"data_residency": "eu"}
    snapshot: Final = price_snapshot(
        {**_BASIS, "expression_policy": {"expression": "p * (attr('data_residency')=='eu' ? 4 : 2)"}},
        _DISCOUNT, "acompletion", request_data={"data_residency": "us"},
        routing_attributes=attributes,
    )
    amount, details = expression_settlement(snapshot, {
        "usage": {"prompt_tokens": 100, "completion_tokens": 0, "data_residency": "us"},
        "data_residency": "us",
    })
    assert Decimal(amount) == Decimal("0.00028")
    assert details["expression_pricing"]["variables"]["attr.data_residency"] == "eu"
    assert "api_base" not in str(snapshot)
    assert "data_residency" not in snapshot["expression_context"]["params"]


def test_mixed_deployment_regions_are_rejected_before_a_catalog_rule_can_be_used() -> None:
    deployments: Final = tuple(
        {"litellm_params": {"model": "openai/fixture", "api_base": base}}
        for base in ("https://eu.api.openai.com/v1", "https://api.openai.com/v1")
    )
    assert catalog_routing_attributes(deployments) == {}
    with pytest.raises(ValueError, match="verified pricing region"):
        price_snapshot(
            {**_BASIS, "expression_policy": {"expression": "p * (attr('data_residency')=='eu' ? 4 : 2)"}},
            _DISCOUNT, "acompletion", routing_attributes=catalog_routing_attributes(deployments),
        )


def test_vertex_region_comes_from_server_deployment_configuration_only() -> None:
    assert catalog_routing_attributes(({
        "litellm_params": {"model": "vertex_ai/fixture", "vertex_location": "us-central1"},
    },)) == {"vertex_location": "us-central1"}
    with pytest.raises(ValueError, match="Response attribute"):
        catalog_routing_attributes(({"litellm_params": {"model": "vertex_ai/fixture", "vertex_location": " "}},))


def test_compatible_gateway_uses_retail_global_rates_unless_deployment_has_an_explicit_region() -> None:
    params: Final = {"model": "openai/fixture", "api_base": "https://compatible.invalid/v1"}
    assert catalog_routing_attributes(({"litellm_params": params},)) == {"data_residency": "global"}
    assert catalog_routing_attributes(({
        "litellm_params": params, "model_info": {"pricing_data_residency": "eu"},
    },)) == {"data_residency": "eu"}


def test_prices_without_routing_conditions_do_not_read_unrelated_deployment_locations() -> None:
    assert catalog_routing_attributes(({
        "litellm_params": {"model": "vertex_ai/fixture", "vertex_location": " "},
    },), frozenset()) == {}


def _snapshot(
    expression: str, started_at: datetime, call_type: str = "acompletion", **policy: object
) -> dict[str, object]:
    return price_snapshot(
        {
            **_BASIS,
            "expression_policy": {
                "version": "expression-v1",
                "expression": expression,
                "timezone": "Asia/Shanghai",
                **policy,
            },
        },
        _DISCOUNT,
        call_type,
        request_data={"service_tier": "default", "messages": [{"role": "user", "content": "private"}]},
        started_at=started_at,
    )


@pytest.mark.parametrize(
    ("started_at", "expected_tier", "coefficient"),
    (
        (datetime(2026, 10, 30, 2, tzinfo=timezone.utc), "weekday peak", "4"),
        (datetime(2026, 10, 30, 15, tzinfo=timezone.utc), "off peak", "2"),
        (datetime(2026, 10, 31, 2, tzinfo=timezone.utc), "off peak", "2"),
    ),
)
def test_settlement_uses_frozen_request_time_timezone_and_discount(
    started_at: datetime,
    expected_tier: str,
    coefficient: str,
) -> None:
    snapshot: Final = _snapshot(
        'weekday() != 0 && weekday() != 6 && hour() >= 9 && hour() < 18 ? tier("weekday peak", p * 4) : tier("off peak", p * 2)',
        started_at,
    )
    amount, details = expression_settlement(snapshot, {"usage": {"prompt_tokens": 1000, "completion_tokens": 0}})
    assert Decimal(amount) == Decimal(coefficient) * Decimal("0.0007")
    assert details["expression_pricing"]["tiers"] == [expected_tier]
    assert details["expression_pricing"]["started_at"] == started_at.isoformat()
    assert "private" not in str(snapshot) and "private" not in str(details)
    assert snapshot["price_version"] == 9


def test_cache_categories_settle_once_with_ttl_and_context_preserved() -> None:
    snapshot: Final = _snapshot(
        'len > 100 ? tier("long", p * 4 + c * 10 + cr * 0.4 + cc * 5 + cc1h * 8) : tier("short", p * 2)',
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    response: Final = {
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 30,
            "cache_creation": {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 10},
        }
    }
    amount, details = expression_settlement(snapshot, response)
    assert (
        Decimal(amount)
        == (Decimal(10 * 4 + 5 * 10 + 80) + Decimal(80) * Decimal("0.4") + Decimal(20 * 5)) * Decimal("0.7") / 1000000
    )
    assert details["expression_pricing"]["variables"]["cc"] == "20"
    assert details["expression_pricing"]["variables"]["len"] == "120"
    quote: Final = reserve_quote(snapshot, {"messages": []}, 5)
    assert Decimal(quote) >= Decimal(amount)


def test_fixed_per_image_charges_actual_output_count_without_token_usage() -> None:
    snapshot: Final = _snapshot(
        'fixed(u("images") * 0.04)',
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        "aimage_generation",
        usage_bounds={"images": "16"},
    )
    amount, details = expression_settlement(
        snapshot, {"data": [{"b64_json": "first"}, {"url": "https://example.invalid/second"}]}
    )
    assert Decimal(amount) == Decimal("0.056")
    assert details["expression_pricing"]["usage"] == {"images": "2"}
    assert Decimal(reserve_quote(snapshot, {}, 1000000)) >= Decimal(amount)


def test_missing_reported_quantities_do_not_turn_an_expression_charge_into_zero() -> None:
    snapshot: Final = _snapshot(
        'fixed(u("audio_seconds") * 0.01)',
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        usage_bounds={"audio_seconds": "600"},
    )
    with pytest.raises(ValueError, match="Missing"):
        expression_settlement(snapshot, {"usage": {"prompt_tokens": 10, "completion_tokens": 2}})
