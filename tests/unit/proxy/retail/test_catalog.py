from datetime import datetime, timezone
from decimal import Decimal
from typing import Final

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import LiteLLM_UserTable, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.route_checks import RouteChecks
from litellm.proxy.retail.catalog import _DiscountRow, build_model_catalog
from litellm.proxy.retail.pool_store import PoolAccess, PoolGrant, _PoolRow, _StoredBinding


def _pool(identifier: str, visibility: str = "public", *, enabled: bool = True) -> _PoolRow:
    return _PoolRow.model_validate(
        {
            "id": identifier,
            "name": identifier,
            "visibility": visibility,
            "enabled": enabled,
            "source_kind": "anthropic",
            "version": 1,
            "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    )


def _binding(pool: str, identity: str, model: str) -> _StoredBinding:
    return _StoredBinding(pool_id=pool, deployment_id=identity, model=model)


def _deployment(identity: str, model: str, **metadata: object) -> dict[str, object]:
    return {
        "model_name": model,
        "model_info": {"id": identity, **metadata},
        "litellm_params": {
            "model": "anthropic/reference-model",
            "api_key": "private-key",
            "api_base": "https://private-upstream.invalid",
        },
    }


def _price(model: str, **rates: object) -> dict[str, object]:
    return {
        "model": model,
        "version": 3,
        "updated_at": "2026-01-01T00:00:00Z",
        "input_cost_per_token": "0.000002",
        "output_cost_per_token": "0.000006",
        "cache_read_input_token_cost": "0.0000002",
        "cache_creation_input_token_cost": "0.0000025",
        **rates,
    }


def _discount(pool_id: str, model: str, discount: str = "0.7", **values: object) -> _DiscountRow:
    return _DiscountRow.model_validate(
        {
            "model": model,
            "pool_id": pool_id,
            "pool_name": pool_id,
            "user_id": "customer",
            "plan_id": "shared",
            "pool_default_discount": "1",
            "customer_discount": discount,
            "plan_discount": None,
            "customer_pool_discount": None,
            "plan_pool_discount": None,
            **values,
        }
    )


def test_expression_models_publish_the_policy_and_discount_without_presenting_inactive_flat_prices() -> None:
    policy: Final = {"version": "expression-v1", "expression": "p * 2 + c * 6", "timezone": "UTC", "usage_bounds": {}}
    page: Final = build_model_catalog(
        (_pool("public"),), (_binding("public", "a", "dynamic"),),
        PoolAccess(mode="custom", grants=(PoolGrant(pool_id="public"),)),
        (_deployment("a", "dynamic"),), (_price("dynamic", expression_policy=policy),),
        (_discount("public", "dynamic"),), {},
    )
    model: Final = page.items[0]
    assert model.expression_policy == policy
    assert model.reference_pricing is None
    assert model.offers[0].pricing.expression_policy == policy
    assert model.offers[0].pricing.reference is None
    assert model.offers[0].pricing.customer is None
    assert model.offers[0].pricing.discount == "0.7"
    assert model.pricing_display is not None
    assert model.pricing_display.token_rates[0].rates == {"p": "2", "c": "6"}
    assert model.offers[0].pricing.pricing_display == model.pricing_display
    serialized: Final = page.model_dump(mode="json")["items"][0]["pricing_display"]
    assert serialized["token_rates"][0]["rates"] == {"p": "2", "c": "6"}
    assert "private" not in page.model_dump_json()


def test_personal_catalog_keeps_only_authorized_pool_model_pairs() -> None:
    pools: Final = (_pool("allowed"), _pool("public"), _pool("private", "restricted"), _pool("draft", enabled=False))
    bindings: Final = (
        _binding("allowed", "a", "shared"),
        _binding("public", "b", "discoverable"),
        _binding("private", "p", "private-granted"),
        _binding("private", "q", "private-forbidden"),
        _binding("draft", "d", "draft-model"),
        _binding("public", "gone", "removed-deployment"),
    )
    access: Final = PoolAccess(
        mode="custom",
        grants=(
            PoolGrant(pool_id="allowed"),
            PoolGrant(pool_id="private", models=("private-granted",)),
        ),
    )
    deployments: Final = tuple(
        _deployment(item.deployment_id, item.model) for item in bindings if item.deployment_id != "gone"
    )
    page: Final = build_model_catalog(pools, bindings, access, deployments, (_price("discoverable"),), (), {})
    entries: Final = {item.id: item for item in page.items}
    assert set(entries) == {"shared", "private-granted"}
    assert entries["shared"].available_to_me is True
    assert entries["private-granted"].available_to_me is True
    text: Final = page.model_dump_json()
    assert all(
        secret not in text for secret in ("private-key", "private-upstream", "reference-model", "private-forbidden")
    )


def test_public_introductions_are_independent_of_pool_access_but_hide_private_and_disabled_offers() -> None:
    model: Final = "shared"
    pools: Final = (_pool("public"), _pool("private", "restricted"), _pool("draft", enabled=False))
    bindings: Final = (
        _binding("public", "a", model),
        _binding("private", "b", "private-model"),
        _binding("draft", "c", "draft-model"),
    )
    access: Final = PoolAccess(mode="custom", grants=(PoolGrant(pool_id="private"),))
    page: Final = build_model_catalog(
        pools,
        bindings,
        access,
        tuple(_deployment(item.deployment_id, item.model) for item in bindings),
        (_price(model),),
        (_discount("public", model),),
        {},
        mode="public",
    )
    entries: Final = {item.id: item for item in page.items}
    assert set(entries) == {model, "private-model", "draft-model"}
    assert entries["private-model"].offers == ()
    assert entries["draft-model"].offers == ()
    item: Final = entries[model]
    assert item.available_to_me is False
    offer: Final = item.offers[0]
    assert offer.pool_id == "public"
    assert offer.available_to_me is False
    assert offer.pricing.reference is not None
    assert offer.pricing.customer is None
    assert offer.pricing.discount is None
    assert all(not entry.available_to_me for entry in page.items)
    assert tuple(offer.pool_id for offer in item.offers) == ("public",)


def test_unbound_models_keep_public_specifications_and_reference_prices_without_exposing_upstream_details() -> None:
    model: Final = "unbound-model"
    metadata: Final = {
        "reference-model": {"mode": "chat", "max_input_tokens": 12345, "supports_function_calling": True}
    }
    page: Final = build_model_catalog(
        (), (), PoolAccess(), (_deployment("a", model),), (_price(model),), (), metadata, mode="public"
    )
    item: Final = page.items[0]
    assert item.id == model
    assert item.offers == ()
    assert item.reference_pricing is not None
    assert Decimal(item.reference_pricing.input or "NaN") == Decimal("2")
    assert item.context_length == metadata["reference-model"]["max_input_tokens"]
    assert item.capabilities.tools is True
    assert item.available_to_me is False
    assert all(
        secret not in page.model_dump_json() for secret in ("private-upstream", "private-key", "reference-model")
    )


def test_tiered_reference_catalog_keeps_base_display_rates_without_inventing_a_flat_customer_quote() -> None:
    model: Final = "tiered-model"
    catalog: Final = {
        "reference-model": {
            "mode": "chat",
            "input_cost_per_token": "0.000002",
            "output_cost_per_token": "0.000006",
            "cache_read_input_token_cost": "0.0000002",
            "cache_creation_input_token_cost": "0.0000025",
            "cache_creation_input_token_cost_above_1hr": "0.000004",
        }
    }
    deployment: Final = _deployment("a", model, input_cost_per_token=999, output_cost_per_token=999)
    page: Final = build_model_catalog(
        (_pool("pool"),),
        (_binding("pool", "a", model),),
        PoolAccess(),
        (deployment,),
        (),
        (_discount("pool", model),),
        catalog,
    )
    item: Final = page.items[0]
    rates: Final = item.reference_pricing
    assert rates is not None
    assert Decimal(rates.input or "NaN") == Decimal(catalog["reference-model"]["input_cost_per_token"]) * 1_000_000
    assert Decimal(rates.output or "NaN") == Decimal(catalog["reference-model"]["output_cost_per_token"]) * 1_000_000
    assert item.offers[0].pricing.reference == rates
    assert item.offers[0].pricing.customer is None


def test_public_canonical_reference_is_independent_of_unrelated_channel_cost_overrides() -> None:
    model: Final = "canonical"
    catalog: Final = {model: {"input_cost_per_token": "0.000003", "output_cost_per_token": "0.000004"}}
    page: Final = build_model_catalog(
        (),
        (),
        PoolAccess(),
        (_deployment("a", model, input_cost_per_token=999, output_cost_per_token=999),),
        (),
        (),
        catalog,
        mode="public",
    )
    rates: Final = page.items[0].reference_pricing
    assert rates is not None
    assert Decimal(rates.input or "NaN") == Decimal(catalog[model]["input_cost_per_token"]) * 1_000_000
    stored: Final = (
        build_model_catalog(
            (), (), PoolAccess(), (_deployment("a", model),), (_price(model),), (), catalog, mode="public"
        )
        .items[0]
        .reference_pricing
    )
    assert stored is not None
    assert Decimal(stored.input or "NaN") == Decimal(str(_price(model)["input_cost_per_token"])) * 1_000_000


@pytest.mark.parametrize("published_write", (None, "0", "0.0000025"))
def test_billing_input_fallback_does_not_invent_a_published_cache_write_price(published_write: str | None) -> None:
    catalog: Final = {"reference-model": {
        "mode": "chat", "litellm_provider": "gemini", "input_cost_per_token": "0.000002",
        "output_cost_per_token": "0.000006", "cache_read_input_token_cost": "0.0000002",
        **({"cache_creation_input_token_cost": published_write} if published_write is not None else {}),
    }}
    page: Final = build_model_catalog(
        (), (), PoolAccess(), (_deployment("a", "public-model"),), (), (), catalog, mode="public",
    )
    rates: Final = page.items[0].reference_pricing
    assert rates is not None
    assert rates.input == "2.000000"
    assert rates.cache_write == (format(Decimal(published_write) * 1_000_000, "f") if published_write is not None else None)


def test_explicit_manual_cache_write_price_remains_public_without_duration_tiers() -> None:
    page: Final = build_model_catalog(
        (), (), PoolAccess(), (_deployment("a", "public-model"),), (_price("public-model"),), (), {}, mode="public",
    )
    rates: Final = page.items[0].reference_pricing
    assert rates is not None
    assert rates.cache_write == "2.5000000"
    assert rates.cache_write_tiers == {}


@pytest.mark.parametrize(
    ("source", "published", "raw_write", "catalog_exists", "visible"),
    (
        ("catalog", False, "0.000003", True, False),
        ("catalog", True, None, True, True),
        ("catalog", None, None, True, False),
        ("catalog", None, "0", True, True),
        ("catalog", None, None, False, False),
        ("manual", None, None, False, True),
        ("legacy", None, None, False, True),
    ),
)
def test_saved_price_publication_survives_catalog_changes_without_repricing_configured_rates(
    source: str, published: bool | None, raw_write: str | None, catalog_exists: bool, visible: bool,
) -> None:
    stored: Final = _price("public-model", source=source, catalog_model="reference-model",
                           cache_write_price_published=published)
    raw: Final = {
        "mode": "chat", "input_cost_per_token": "0.000001", "output_cost_per_token": "0.000009",
        **({"cache_creation_input_token_cost": raw_write} if raw_write is not None else {}),
    }
    page: Final = build_model_catalog(
        (_pool("pool"),), (_binding("pool", "a", "public-model"),), PoolAccess(),
        (_deployment("a", "public-model"),), (stored,), (_discount("pool", "public-model"),),
        {"reference-model": raw} if catalog_exists else {},
    )
    item: Final = page.items[0]
    assert item.reference_pricing is not None
    assert item.reference_pricing.input == "2.000000"
    assert item.reference_pricing.cache_write == ("2.5000000" if visible else None)
    customer: Final = item.offers[0].pricing.customer
    assert customer is not None
    assert customer.cache_write == ("1.75000000" if visible else None)
    assert stored["cache_creation_input_token_cost"] == "0.0000025"
    assert stored["version"] == 3
    assert stored["cache_write_price_published"] is published


def test_hidden_generic_publication_keeps_explicit_duration_prices_and_free_rates() -> None:
    stored: Final = _price("public-model", source="catalog", cache_write_price_published=False,
                           cache_write_tiers={"300": "0", "3600": "0.000004"})
    page: Final = build_model_catalog(
        (), (), PoolAccess(), (_deployment("a", "public-model"),), (stored,), (), {}, mode="public",
    )
    rates: Final = page.items[0].reference_pricing
    assert rates is not None and rates.cache_write is None
    assert rates.cache_write_tiers == {"300": "0", "3600": "4.000000"}


def test_public_model_names_are_deduplicated_and_wildcards_are_not_presented_as_models() -> None:
    deployments: Final = (
        _deployment("a", "model-a"),
        _deployment("b", "model-a"),
        _deployment("c", "*"),
        _deployment("d", "provider/*"),
        _deployment("e", " "),
    )
    page: Final = build_model_catalog((), (), PoolAccess(), deployments, (), (), {}, mode="public")
    assert tuple(item.id for item in page.items) == ("model-a",)


def test_personal_grant_to_one_model_does_not_expose_other_models_in_the_same_public_pool() -> None:
    bindings: Final = (_binding("pool", "a", "allowed"), _binding("pool", "b", "other"))
    access: Final = PoolAccess(mode="custom", grants=(PoolGrant(pool_id="pool", models=("allowed",)),))
    page: Final = build_model_catalog(
        (_pool("pool"),),
        bindings,
        access,
        tuple(_deployment(item.deployment_id, item.model) for item in bindings),
        (),
        (),
        {},
    )
    assert tuple(item.id for item in page.items) == ("allowed",)


def test_same_public_model_keeps_pool_specific_customer_prices_and_reference_provenance() -> None:
    model: Final = "shared"
    pools: Final = (_pool("first"), _pool("second"))
    bindings: Final = (_binding("first", "a", model), _binding("second", "b", model))
    deployments: Final = (_deployment("a", model), _deployment("b", model))
    discounts: Final = (_discount("first", model), _discount("second", model, "0.4"))
    item: Final = build_model_catalog(
        pools, bindings, PoolAccess(), deployments, (_price(model),), discounts, {}
    ).items[0]
    first, second = item.offers
    assert first.pricing.reference == second.pricing.reference
    assert first.pricing.customer is not None
    assert second.pricing.customer is not None
    assert Decimal(first.pricing.customer.input or "NaN") == Decimal("2") * Decimal("0.7")
    assert Decimal(second.pricing.customer.input or "NaN") == Decimal("2") * Decimal("0.4")
    assert first.pricing.version == _price(model)["version"]
    assert first.pricing.updated_at == _price(model)["updated_at"]
    assert first.pricing.source_label == "Configured reference price"


def test_catalog_quote_uses_the_same_single_discount_precedence_as_pool_settlement() -> None:
    from litellm.proxy.retail.pool_pricing import pool_discount_view

    model: Final = "shared"
    factors: Final = _discount("first", model, "0.73", plan_discount="0.6", customer_pool_discount="0.4")
    item: Final = build_model_catalog(
        (_pool("first"),),
        (_binding("first", "a", model),),
        PoolAccess(),
        (_deployment("a", model),),
        (_price(model),),
        (factors,),
        {},
    ).items[0]
    pricing: Final = item.offers[0].pricing
    assert pricing.discount == pool_discount_view(model, factors)["discount"]
    assert pricing.customer is not None
    assert Decimal(pricing.customer.output or "NaN") == Decimal("6") * Decimal(str(pricing.discount))


def test_missing_prices_and_metadata_remain_unknown_instead_of_zero_or_inferred_support() -> None:
    item: Final = build_model_catalog(
        (_pool("pool"),),
        (_binding("pool", "a", "unlisted"),),
        PoolAccess(),
        (_deployment("a", "unlisted", description="private-operator-notes"),),
        (),
        (),
        {},
    ).items[0]
    assert item.description is None
    assert item.context_length is None
    assert item.capabilities.tools is None
    assert item.input_modalities == ()
    assert item.offers[0].pricing.reference is None
    assert item.offers[0].pricing.customer is None
    assert "private-operator-notes" not in item.model_dump_json()


def test_disagreeing_deployments_do_not_advertise_a_capability_or_context_they_do_not_share() -> None:
    model: Final = "shared"
    metadata: Final = {
        "reference-model": {
            "mode": "chat",
            "max_input_tokens": 8000,
            "max_output_tokens": 2000,
            "supports_function_calling": True,
            "supported_modalities": ["text", "image"],
        }
    }
    deployments: Final = (
        _deployment("a", model),
        _deployment("b", model, max_input_tokens=4000, supports_function_calling=False),
    )
    item: Final = build_model_catalog(
        (_pool("pool"),),
        (_binding("pool", "a", model), _binding("pool", "b", model)),
        PoolAccess(),
        deployments,
        (),
        (),
        metadata,
    ).items[0]
    assert item.context_length is None
    assert item.capabilities.tools is None
    assert item.max_output_tokens == metadata["reference-model"]["max_output_tokens"]
    assert item.input_modalities == ("image", "text")
    assert item.metadata_source == "reference_catalog"


def test_nullable_deployment_defaults_do_not_erase_known_catalog_facts() -> None:
    model: Final = "shared"
    metadata: Final = {"reference-model": {"mode": "chat", "max_input_tokens": 9000, "supports_function_calling": True}}
    item: Final = build_model_catalog(
        (_pool("pool"),),
        (_binding("pool", "a", model),),
        PoolAccess(),
        (_deployment("a", model, max_input_tokens=None, supports_function_calling=None),),
        (),
        (),
        metadata,
    ).items[0]
    assert item.context_length == metadata["reference-model"]["max_input_tokens"]
    assert item.capabilities.tools is True


def test_zero_price_is_preserved_as_free_and_decimal_customer_quote_does_not_round_through_float() -> None:
    model: Final = "shared"
    card: Final = _price(model, input_cost_per_token="0", output_cost_per_token="0.000000001")
    item: Final = build_model_catalog(
        (_pool("pool"),),
        (_binding("pool", "a", model),),
        PoolAccess(),
        (_deployment("a", model),),
        (card,),
        (_discount("pool", model, "0.333333333"),),
        {},
    ).items[0]
    pricing: Final = item.offers[0].pricing
    assert pricing.reference is not None and Decimal(pricing.reference.input or "NaN") == 0
    assert pricing.customer is not None
    assert Decimal(pricing.customer.output or "NaN") == Decimal("0.001") * Decimal("0.333333333")


@pytest.mark.parametrize("role", (LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.INTERNAL_USER_VIEW_ONLY))
def test_catalog_is_available_to_authenticated_customer_ui_sessions(role: LitellmUserRoles) -> None:
    auth: Final = UserAPIKeyAuth(user_id="customer", user_role=role, team_id=UI_SESSION_TOKEN_TEAM_ID)
    request: Final = Request({"type": "http", "method": "GET", "path": "/retail/model-catalog"})
    RouteChecks.non_proxy_admin_allowed_routes_check(
        user_obj=LiteLLM_UserTable(user_id="customer"),
        _user_role=role,
        route="/retail/model-catalog",
        request=request,
        valid_token=auth,
        request_data={},
    )


def test_catalog_does_not_expand_personal_ai_key_management_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "true")
    auth: Final = UserAPIKeyAuth(
        user_id="customer", user_role=LitellmUserRoles.INTERNAL_USER, allowed_routes=["llm_api_routes"]
    )
    with pytest.raises(HTTPException) as failure:
        RouteChecks.is_virtual_key_allowed_to_call_route(route="/retail/model-catalog", valid_token=auth)
    assert failure.value.status_code == 403


@pytest.mark.asyncio
async def test_public_catalog_auth_gate_accepts_anonymous_callers_before_key_lookup() -> None:
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth_builder

    request: Final = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/retail/public/model-catalog",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
        }
    )
    auth: Final = await user_api_key_auth_builder(
        request=request,
        api_key="",
        azure_api_key_header="",
        anthropic_api_key_header=None,
        google_ai_studio_api_key_header=None,
        azure_apim_header=None,
        request_data={},
    )
    assert auth.user_id is None
    assert auth.user_role == LitellmUserRoles.INTERNAL_USER_VIEW_ONLY
