from datetime import datetime
from decimal import Decimal
from typing import Final

import pytest

from litellm.proxy.retail.catalog_verified_rules import apply_catalog_calendar, enrich_catalog_rates
from litellm.proxy.retail.expression_pricing import (
    evaluate_expression,
    freeze_expression_context,
    validate_expression_policy,
)

_FIELDS: Final = ("input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost")
# Official schedule: https://ai.google.dev/gemini-api/docs/pricing, verified 2026-11-02
_ARGON: Final = {
    "litellm_provider": "gemini",
    "input_cost_per_token": "0.000002",
    "output_cost_per_token": "0.000010",
    "cache_read_input_token_cost": "0.0000001",
}


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai", "vertex_ai-language-models"))
@pytest.mark.parametrize("service", ("batches", "flex"))
@pytest.mark.parametrize("field", _FIELDS)
def test_missing_gemini_pro_long_service_rates_preserve_the_existing_tier_ratio(
    provider: str,
    service: str,
    field: str,
) -> None:
    metadata: Final = {
        "litellm_provider": provider,
        field: "0.000004",
        f"{field}_above_200k_tokens": "0.000010",
        f"{field}_{service}": "0.000002",
    }
    enriched: Final = enrich_catalog_rates("gemini/gemini-2.5-pro", metadata)
    derived: Final = Decimal(str(enriched[f"{field}_above_200k_tokens_{service}"]))
    assert derived / Decimal(str(metadata[f"{field}_above_200k_tokens"])) == (
        Decimal(str(metadata[f"{field}_{service}"])) / Decimal(str(metadata[field]))
    )
    assert f"{field}_above_200k_tokens_{service}" not in metadata


@pytest.mark.parametrize("explicit", ("0", "0.000003", None))
def test_explicit_source_long_service_rate_is_never_overwritten(explicit: object) -> None:
    target: Final = "input_cost_per_token_above_200k_tokens_batches"
    metadata: Final = {
        "litellm_provider": "gemini",
        "input_cost_per_token": "0.000004",
        "input_cost_per_token_above_200k_tokens": "0.000010",
        "input_cost_per_token_batches": "0.000002",
        target: explicit,
    }
    assert enrich_catalog_rates("gemini-2.5-pro", metadata) is metadata


@pytest.mark.parametrize(
    ("model", "provider"),
    (
        ("gemini-2.5-pro-preview", "gemini"),
        ("gemini-3.1-pro", "gemini"),
        ("gemini-3-flash", "gemini"),
        ("gemini-2.5-pro", "openai"),
    ),
)
def test_unverified_names_and_providers_do_not_inherit_gemini_pro_rates(model: str, provider: str) -> None:
    metadata: Final = {
        "litellm_provider": provider,
        "input_cost_per_token": "0.000004",
        "input_cost_per_token_above_200k_tokens": "0.000010",
        "input_cost_per_token_batches": "0.000002",
    }
    assert enrich_catalog_rates(model, metadata) is metadata


@pytest.mark.parametrize("invalid", (None, True, "NaN", "Infinity", "-1", "x", "0"))
def test_missing_or_invalid_base_rate_does_not_create_a_derived_price(invalid: object) -> None:
    metadata: Final = {
        "litellm_provider": "gemini",
        "input_cost_per_token": invalid,
        "input_cost_per_token_above_200k_tokens": "0.000010",
        "input_cost_per_token_flex": "0.000002",
    }
    assert enrich_catalog_rates("gemini-2.5-pro", metadata) is metadata


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai", "vertex_ai-language-models"))
@pytest.mark.parametrize(
    ("started_at", "factor", "label"),
    (
        ("2026-11-30T23:59:59+00:00", "1", "introductory through 2026-11-30"),
        ("2026-12-01T00:00:00+00:00", "2", "standard from 2026-12-01"),
        ("2026-12-01T08:00:00+08:00", "2", "standard from 2026-12-01"),
    ),
)
def test_verified_argon_calendar_uses_the_frozen_utc_date_and_preserves_tool_fees(
    provider: str,
    started_at: str,
    factor: str,
    label: str,
) -> None:
    token: Final = {"expression": "p * 2 + c * 10 + cr * 0.1", "timezone": "America/Los_Angeles"}
    scheduled: Final = apply_catalog_calendar("gemini/gemini-4-argon", {**_ARGON, "litellm_provider": provider}, token)
    assert scheduled is not None
    policy: Final = validate_expression_policy(
        {**scheduled, "expression": f"({scheduled['expression']}) + fixed(0.04)"}
    )
    context: Final = freeze_expression_context(policy, {}, datetime.fromisoformat(started_at))
    result: Final = evaluate_expression(policy, {"p": 100_000, "c": 10_000, "cr": 100_000}, context)
    assert Decimal(str(result["amount"])) == Decimal("0.31") * Decimal(factor) + Decimal("0.04")
    assert any(isinstance(tier, str) and label in tier for tier in result["tiers"])
    assert policy.timezone == token["timezone"]


def test_already_updated_argon_source_prices_are_not_doubled_again() -> None:
    metadata: Final = {**_ARGON, **{key: str(Decimal(str(_ARGON[key])) * 2) for key in _FIELDS}}
    policy: Final = {"expression": "p * 4 + c * 20 + cr * 0.2", "usage_bounds": {"images": "2"}}
    returned: Final = apply_catalog_calendar("gemini-4-argon", metadata, policy)
    assert returned == validate_expression_policy(policy).model_dump(mode="json")


@pytest.mark.parametrize("field", _FIELDS)
def test_conflicting_argon_source_prices_require_review(field: str) -> None:
    assert apply_catalog_calendar("gemini-4-argon", {**_ARGON, field: "0.123"}, {"expression": "p * 2"}) is None


def test_calendar_leaves_other_model_policies_unchanged() -> None:
    policy: Final = {"expression": "p * 2", "timezone": "UTC"}
    assert apply_catalog_calendar("gemini-3-flash", _ARGON, policy) == validate_expression_policy(policy).model_dump(
        mode="json"
    )
    assert apply_catalog_calendar("gemini-4-argon", {**_ARGON, "litellm_provider": "other"}, policy) == (
        validate_expression_policy(policy).model_dump(mode="json")
    )


def test_invalid_calendar_input_policy_cannot_generate_a_billable_rule() -> None:
    assert apply_catalog_calendar("gemini-4-argon", _ARGON, {"expression": "unknown()"}) is None
