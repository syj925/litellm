from typing import Final

import pytest

from litellm.proxy.retail.model_price_catalog import PricingCatalogPrice, build_pricing_catalog
from litellm.proxy.retail.pricing import catalog_snapshot


def test_saved_expression_policy_is_the_active_price_in_the_admin_catalog() -> None:
    policy: Final = {"version": "expression-v1", "expression": "p * 2 + c * 6", "timezone": "UTC", "usage_bounds": {}}
    saved: Final = PricingCatalogPrice(
        model="dynamic", input_per_million="9", output_per_million="9", cache_read_per_million="9",
        cache_write_per_million="9", source="manual", source_label="Reviewed expression", expression_policy=policy,
    )
    row: Final = build_pricing_catalog({}, (saved,), {}, "Catalog fixture").items[0]
    assert row.status == "expression"
    assert row.price == saved
    assert row.price.expression_policy == policy


@pytest.mark.parametrize(("provider", "extra", "expected"), (
    ("openai", {"regional_processing_uplift_multiplier_asia": "1.1"}, ("regional_processing_uplift_multiplier_asia",)),
    ("anthropic", {"provider_specific_entry": {"us": "1.1", "future": "2"}}, ("provider_specific_entry",)),
))
def test_unmapped_catalog_dimensions_name_the_exact_fields_without_offering_a_partial_policy(
    provider: str, extra: dict[str, object], expected: tuple[str, ...],
) -> None:
    metadata: Final = {
        "mode": "chat", "litellm_provider": provider,
        "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000006",
        "search_context_cost_per_query": {
            "search_context_size_low": "0.0123456789", "search_context_size_medium": "0.0123456789",
            "search_context_size_high": "0.0123456789",
        }, **extra,
    }
    row: Final = build_pricing_catalog(
        {"public-model": ({"litellm_params": {"model": "fixture"}},)}, (), {"fixture": metadata}, "Catalog fixture",
    ).items[0]
    assert row.status == "unavailable" and row.price is None
    assert row.reason == "Manual pricing is required for unresolved catalog fields: " + ", ".join(expected)
    assert row.review_references[0].expression_policy is None
    assert "0.0123456789" not in row.reason and "1.1" not in row.reason


def test_shared_free_allowance_note_is_visible_on_a_complete_paid_rate_rule() -> None:
    metadata: Final = {
        "litellm_provider": "gemini", "mode": "chat",
        "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000006",
        "search_context_cost_per_query": {"search_context_size_medium": "0.012"},
        "search_free_requests": {"count": 10, "period": "day", "shared_group": "fixture"},
    }
    row: Final = build_pricing_catalog(
        {"public-model": ({"litellm_params": {"model": "fixture"}},)}, (), {"fixture": metadata}, "Catalog fixture",
    ).items[0]
    assert row.status == "expression" and row.price is not None
    assert row.reason is not None and "free allowances are not passed through" in row.reason
    assert "paid rates" in row.reason
    assert row.price.expression_policy["usage_bounds"]["web_search_requests"] == "64"


def test_manual_review_reasons_bound_field_counts_and_never_copy_urls_or_values() -> None:
    fields: Final = {f"unsupported_fee_field_{index:02d}": "https://private.invalid/value" for index in range(20)}
    metadata: Final = {
        "mode": "chat", "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000006", **fields,
        "https://private.invalid/pricing": "private-value",
    }
    row: Final = build_pricing_catalog(
        {"public-model": ({"litellm_params": {"model": "fixture"}},)}, (), {"fixture": metadata}, "Catalog fixture",
    ).items[0]
    assert row.reason is not None and len(row.reason) <= 2048
    assert "unsupported_fee_field_00" in row.reason and "unsupported_fee_field_15" in row.reason
    assert "4 more fields" in row.reason
    assert "unsupported_fee_field_16" not in row.reason
    assert "https" not in row.reason and "private" not in row.reason


def test_configured_models_show_catalog_defaults_without_saving_or_exposing_channel_configuration() -> None:
    catalog: Final = {
        "anthropic/fixture": {
            "litellm_provider": "anthropic", "mode": "chat",
            "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010",
            "cache_read_input_token_cost": "0", "cache_creation_input_token_cost": "0.0000025",
            "cache_creation_input_token_cost_above_1hr": "0.000004",
        },
    }
    deployments: Final = (
        {"litellm_params": {"model": "anthropic/fixture", "api_key": "private", "api_base": "private"}},
        {"litellm_params": {"model": "anthropic/fixture", "input_cost_per_token": 99}},
    )
    page: Final = build_pricing_catalog({"public-name": deployments}, (), catalog, "Catalog fixture")
    row: Final = page.items[0]
    snapshot: Final = catalog_snapshot("public-name", deployments, "1", catalog)
    assert row.model == "public-name"
    assert row.status == "default"
    assert row.providers == ("anthropic",)
    assert row.modes == ("chat",)
    assert row.configured is True
    assert row.price == row.candidates[0]
    assert row.price is not None
    assert row.price.model == snapshot["model"]
    assert row.price.catalog_model == snapshot["catalog_model"]
    assert row.price.input_per_million == "2.000000"
    assert row.price.cache_read_per_million == "0"
    assert row.price.cache_write_tiers == {"300": "2.5000000", "3600": "4.000000"}
    assert row.price.cache_write_price_published is True
    assert row.price.version is None
    assert "private" not in page.model_dump_json()


@pytest.mark.parametrize("source", ("manual", "catalog", "legacy"))
def test_saved_overrides_take_precedence_and_override_only_models_remain_editable(source: str) -> None:
    saved: Final = PricingCatalogPrice.model_validate({
        "model": "public-name", "input_per_million": "0", "output_per_million": "12",
        "cache_read_per_million": "0.2", "cache_write_per_million": "3",
        "cache_write_tiers": {"75": "0"}, "cache_write_price_published": False,
        "source": source, "source_label": "Reviewed rate", "version": 3,
        "updated_at": "2026-01-01T00:00:00Z",
    })
    configured: Final = {"public-name": ({"litellm_params": {"model": "openai/fixture"}},)}
    catalog: Final = {"fixture": {"input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010"}}
    current: Final = build_pricing_catalog(configured, (saved,), catalog, "Catalog fixture").items[0]
    inactive: Final = build_pricing_catalog({}, (saved,), catalog, "Catalog fixture").items[0]
    assert current.price == saved
    assert current.status == ("catalog" if source == "catalog" else "custom")
    assert current.configured is True
    assert current.price.cache_write_price_published is False
    assert current.price.cache_write_tiers == {"75": "0"}
    assert current.price.input_per_million == "0"
    assert inactive.price == saved
    assert inactive.configured is False


@pytest.mark.parametrize("second", ("fixture-two", "missing"))
def test_ambiguous_or_partially_unresolved_deployments_have_no_active_price(second: str) -> None:
    catalog: Final = {
        "fixture-one": {"input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010"},
        "fixture-two": {"input_cost_per_token": "0.000003", "output_cost_per_token": "0.000015"},
    }
    configured: Final = {"public-name": (
        {"litellm_params": {"model": "openai/fixture-one"}}, {"litellm_params": {"model": second}},
    )}
    row: Final = build_pricing_catalog(configured, (), catalog, "Catalog fixture").items[0]
    assert row.status == "ambiguous"
    assert row.price is None
    assert len(row.candidates) == (2 if second == "fixture-two" else 1)
    assert row.reason is not None and "Choose a reference" in row.reason


def test_supported_context_tiers_get_an_expression_while_unsupported_prices_have_no_active_rates() -> None:
    catalog: Final = {
        "image": {"litellm_provider": "vendor", "mode": "image_generation", "input_cost_per_pixel": "0.1"},
        "tiered": {
            "litellm_provider": "vendor", "mode": "chat", "input_cost_per_token": "0.000002",
            "output_cost_per_token": "0.000010", "input_cost_per_token_above_200k_tokens": "0.000004",
        },
    }
    configured: Final = {
        model: ({"litellm_params": {"model": f"vendor/{model}"}},) for model in ("image", "tiered", "missing")
    }
    page: Final = build_pricing_catalog(configured, (), catalog, "Catalog fixture")
    assert tuple(row.model for row in page.items) == ("image", "missing", "tiered")
    assert all(row.status == "unavailable" and row.price is None and row.candidates == () for row in page.items[:2])
    assert page.items[0].modes == ("image_generation",)
    assert page.items[1].providers == ("vendor",)
    dynamic: Final = page.items[2]
    assert dynamic.status == "expression"
    assert dynamic.price is not None and dynamic.price.expression_policy is not None
    assert dynamic.review_references[0].expression_policy == dynamic.price.expression_policy


def test_internal_fallback_write_price_retains_its_unpublished_flag() -> None:
    catalog: Final = {"fixture": {
        "mode": "embedding", "litellm_provider": "vendor", "input_cost_per_token": "0.000002",
    }}
    row: Final = build_pricing_catalog(
        {"public-embedding": ({"litellm_params": {"model": "vendor/fixture"}},)}, (), catalog, "Catalog fixture",
    ).items[0]
    assert row.price is not None
    assert row.price.output_per_million == "0"
    assert row.price.cache_write_per_million == row.price.input_per_million
    assert row.price.cache_write_price_published is False
    assert row.price.cache_write_tiers == {}


@pytest.mark.parametrize("published", (None, False, True))
def test_saved_catalog_prices_resolve_unknown_publication_without_replacing_explicit_evidence(published: bool | None) -> None:
    saved: Final = PricingCatalogPrice(
        model="public-model", input_per_million="2", output_per_million="6", cache_read_per_million="0.2",
        cache_write_per_million="2", source="catalog", catalog_model="fixture", source_label="Earlier catalog",
        cache_write_price_published=published,
    )
    catalog: Final = {"fixture": {
        "mode": "chat", "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000006",
    }}
    row: Final = build_pricing_catalog({}, (saved,), catalog, "Catalog fixture").items[0]
    assert row.price is not None
    assert row.price.cache_write_per_million == saved.cache_write_per_million
    assert row.price.cache_write_price_published is (False if published is None else published)


def test_tiered_catalog_rates_remain_reviewable_without_becoming_effective_flat_prices() -> None:
    rates: Final = {
        "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010",
        "cache_read_input_token_cost": "0", "cache_creation_input_token_cost": "0.0000025",
        "cache_creation_input_token_cost_above_12hr": "0.000005",
        "cache_creation_input_token_cost_above_12hr_ultrafast": "0.000015",
        "cache_creation_input_token_cost_above_12hr_batches": "0.0000025",
        "cache_creation_input_token_cost_above_12hr_flex": "0.0000025",
        "cache_creation_input_token_cost_above_12hr_priority": "0.000010",
        "input_cost_per_token_above_272k_tokens": "0.000004",
        "output_cost_per_token_above_272k_tokens": "0.000020",
        "cache_read_input_token_cost_above_272k_tokens": "0.0000002",
    }
    catalog: Final = {"openai/tiered": {
        **rates, "mode": "chat", "litellm_provider": "openai", "api_base": "private-metadata",
        "invalid_cost": "NaN", "negative_cost": "-1", "flag_cost": True, "nested_cost": {"price": 1},
    }}
    row: Final = build_pricing_catalog(
        {"public-tiered": ({"litellm_params": {
            "model": "openai/tiered", "api_key": "private-key", "input_cost_per_token": 99,
        }},)}, (), catalog, "Catalog fixture",
    ).items[0]
    assert row.price is None
    assert row.candidates == ()
    assert row.status == "unavailable"
    assert len(row.review_references) == 1
    review: Final = row.review_references[0]
    assert review.catalog_model == "openai/tiered"
    assert review.rates == rates
    assert review.base_price is not None
    assert review.base_price.model == row.model
    assert review.base_price.input_per_million == "2.000000"
    assert review.base_price.output_per_million == "10.000000"
    assert review.base_price.cache_write_tiers == {"1800": "2.5000000", "43200": "5.000000"}
    assert "private" not in row.model_dump_json()


@pytest.mark.parametrize("mode", ("image_generation", "video_generation"))
def test_non_token_catalog_rates_keep_their_original_units_and_have_no_token_edit_preview(mode: str) -> None:
    rates: Final = {"input_cost_per_image": "0.05", "output_cost_per_second": "0.1"}
    row: Final = build_pricing_catalog(
        {"public-media": ({"litellm_params": {"model": "vendor/media"}},)}, (),
        {"vendor/media": {"mode": mode, **rates}}, "Catalog fixture",
    ).items[0]
    assert row.price is None and row.candidates == ()
    assert row.review_references[0].rates == rates
    assert row.review_references[0].base_price is None


def test_review_preview_uses_only_canonical_token_rates_and_keeps_other_units_for_review() -> None:
    rates: Final = {
        "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010",
        "input_cost_per_video_per_second_above_8s_interval": "0.1",
    }
    row: Final = build_pricing_catalog(
        {"public-video": ({"litellm_params": {"model": "vendor/video"}},)}, (),
        {"vendor/video": {"mode": "chat", **rates}}, "Catalog fixture",
    ).items[0]
    assert row.price is None
    assert row.review_references[0].rates == rates
    assert row.review_references[0].base_price is not None
    assert row.review_references[0].base_price.input_per_million == "2.000000"
    assert row.review_references[0].base_price.output_per_million == "10.000000"


def test_review_references_keep_distinct_deployment_models_and_ignore_unresolved_names() -> None:
    rates: Final = {"mode": "chat", "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010"}
    catalog: Final = {"one": rates, "two": {**rates, "input_cost_per_token": "0.000003"}}
    row: Final = build_pricing_catalog(
        {"public-name": tuple({"litellm_params": {"model": model}} for model in ("one", "two", "one", "unknown"))},
        (), catalog, "Catalog fixture",
    ).items[0]
    assert row.price is None and row.status == "ambiguous"
    assert tuple(review.catalog_model for review in row.review_references) == ("one", "two")
    assert tuple(review.base_price.input_per_million for review in row.review_references if review.base_price) == (
        "2.000000", "3.000000",
    )
