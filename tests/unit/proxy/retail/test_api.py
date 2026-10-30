import hashlib
from decimal import Decimal
from typing import Final

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.retail.api import (
    ModelPriceRequest,
    PlanRequest,
    PricingPreviewRequest,
    ReferenceImportRequest,
    ResolveRequest,
    SetDiscountRequest,
    SetModelDiscountRequest,
    SignupRequest,
    _model_price_view,
    _per_token_price,
    _public_ledger_details,
    _PublicLedgerPage,
    _select_references,
    _source_hash,
    _StoredModelPrice,
    _validate_discount_selection,
    retail_admin,
    retail_user,
    router,
)


def test_custom_cache_duration_prices_keep_per_million_units_and_zero_rates() -> None:
    request: Final = ModelPriceRequest(
        model="example", input_per_million="2", output_per_million="10",
        cache_read_per_million="0.2", cache_write_per_million="2.5",
        cache_write_tiers={"300": "2.5", "3600": "4", "750": "0"},
    )
    assert request.cache_write_tiers == {"300": "2.5", "3600": "4", "750": "0"}
    assert _per_token_price(request.cache_write_tiers["750"]) == "0"
    assert _per_token_price(request.cache_write_tiers["3600"]) == "0.000004"


@pytest.mark.parametrize("published", (None, False, True))
def test_admin_price_edits_can_preserve_unpublished_cache_defaults_or_publish_an_explicit_zero(published: bool | None) -> None:
    request: Final = ModelPriceRequest.model_validate({
        "model": "fixture", "input_per_million": "1", "output_per_million": "2",
        "cache_read_per_million": "0.1", "cache_write_per_million": "0",
        "cache_write_price_published": published,
    })
    assert request.cache_write_price_published is published
    assert request.cache_write_per_million == "0"


@pytest.mark.parametrize("published", ("false", 0, 1, [], {}))
def test_admin_price_edit_publication_flags_are_strict_booleans(published: object) -> None:
    with pytest.raises(ValidationError, match="cache_write_price_published"):
        ModelPriceRequest.model_validate({
            "model": "fixture", "input_per_million": "1", "output_per_million": "2",
            "cache_read_per_million": "0.1", "cache_write_per_million": "0",
            "cache_write_price_published": published,
        })


def test_management_price_responses_keep_internal_default_amounts_separate_from_publication_evidence() -> None:
    view: Final = _model_price_view(_StoredModelPrice(
        model="fixture", version=3, updated_at="2026-01-01T00:00:00Z", source="catalog",
        input_cost_per_token="0.000002", output_cost_per_token="0.000006",
        cache_read_input_token_cost="0.0000002", cache_creation_input_token_cost="0.000002",
        cache_write_price_published=False,
    ))
    assert view["cache_write_per_million"] == "2.000000"
    assert view["cache_write_price_published"] is False
    assert view["version"] == 3


@pytest.mark.parametrize("tiers", ({"0": "1"}, {"0300": "1"}, {"5m": "1"}, {"300": "NaN"}, {"300": "-1"}))
def test_custom_cache_duration_prices_reject_ambiguous_or_invalid_rows(tiers: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ModelPriceRequest(
            model="example", input_per_million="2", output_per_million="10",
            cache_read_per_million="0.2", cache_write_per_million="2.5", cache_write_tiers=tiers,
        )


def test_customer_bill_preserves_safe_duration_counts_prices_and_costs_without_internal_data() -> None:
    details: Final = _public_ledger_details({
        "usage": {
            "cache_write_tokens": 11, "cache_write_ttl_tokens": {"300": 4, "3600": 6, "secret": 999},
            "cache_write_unclassified_tokens": 1, "provider_secret": "private",
        },
        "price_snapshot": {"cache_write_tiers": {"300": "0.0000025", "3600": "0.000004"}, "provider_cost": "private"},
        "cost_breakdown": {"input": "0.1", "cache_write_tiers": {"300": "0.2"}, "upstream": "private"},
        "provider_cost": "private",
    })
    assert details["usage"] == {
        "cache_write_tokens": 11, "cache_write_unclassified_tokens": 1,
        "cache_write_ttl_tokens": {"300": 4, "3600": 6},
    }
    assert details["price_snapshot"] == {"cache_write_tiers": {"300": "0.0000025", "3600": "0.000004"}}
    assert details["cost_breakdown"] == {"input": "0.1", "cache_write_tiers": {"300": "0.2"}}
    assert "private" not in str(details)


@pytest.mark.parametrize("policy", (
    "p * 2", {"version": "expression-v0", "expression": "p * 2"},
    {"version": "expression-v1", "expression": "__import__('os')"},
    {"version": "expression-v1", "expression": "p * 2", "request": {"api_key": "private"}},
))
def test_admin_price_edits_reject_unsafe_or_malformed_expression_policies(policy: object) -> None:
    with pytest.raises(ValidationError):
        ModelPriceRequest.model_validate({
            "model": "fixture", "input_per_million": "1", "output_per_million": "2",
            "cache_read_per_million": "0.1", "cache_write_per_million": "0", "expression_policy": policy,
        })


@pytest.mark.parametrize("facts", ({"p": True}, {"p": 0.5}, {"usage": {"images": False}}))
def test_pricing_preview_rejects_coerced_or_lossy_usage_values(facts: object) -> None:
    with pytest.raises(ValidationError):
        PricingPreviewRequest.model_validate({
            "expression_policy": {"version": "expression-v1", "expression": "p * 2"},
            "facts": facts, "started_at": "2026-01-01T00:00:00Z",
        })


def test_pricing_preview_requires_an_explicit_timezone_in_the_frozen_start_time() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        PricingPreviewRequest.model_validate({
            "expression_policy": {"version": "expression-v1", "expression": "p * 2"},
            "facts": {"p": "100"}, "started_at": "2026-01-01T00:00:00",
        })


@pytest.mark.asyncio
async def test_pricing_preview_evaluates_the_real_engine_without_storage_and_preserves_the_requested_time() -> None:
    async def administrator() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="preview-admin", user_role=LitellmUserRoles.PROXY_ADMIN)

    app: Final = FastAPI()
    app.include_router(router)
    app.dependency_overrides[user_api_key_auth] = administrator
    payload: Final = {
        "expression_policy": {
            "version": "expression-v1", "expression": "p * 2 + c * 6 + cache_write('750') * 3 + 0.0003", "timezone": "UTC",
        },
        "facts": {"p": "100", "c": 10, "cache_write_ttl_tokens": {"750": "100"}},
        "discount": "0.7", "started_at": "2026-01-01T20:30:00+08:00",
        "request": {"api_key": "private-preview-key", "messages": [{"role": "user", "content": "private-message"}]},
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retail.test") as client:
        response: Final = await client.post("/retail/admin/pricing-preview", json=payload)
    assert response.status_code == 200, response.text
    result: Final = response.json()
    assert Decimal(result["undiscounted_amount"]) == Decimal("0.0005600003")
    assert Decimal(result["amount"]) == Decimal("0.000392001")
    assert Decimal(result["rounding_adjustment"]) == Decimal("0.00000000079")
    assert result["started_at"] == payload["started_at"]
    assert result["timezone"] == "UTC"
    assert result["variables"] == {"p": "100", "c": "10", "cache_write_750": "100"}
    assert "private" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", (LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY))
async def test_pricing_preview_requires_an_administrator_even_though_it_is_read_only(role: LitellmUserRoles) -> None:
    async def authenticated_user() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="preview-reader", user_role=role)

    app: Final = FastAPI()
    app.include_router(router)
    app.dependency_overrides[user_api_key_auth] = authenticated_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retail.test") as client:
        response: Final = await client.post("/retail/admin/pricing-preview", json={
            "expression_policy": {"version": "expression-v1", "expression": "p * 2"},
            "facts": {"p": "100"}, "started_at": "2026-01-01T00:00:00Z",
        })
    assert response.status_code == 403, response.text


def test_expression_receipts_keep_verified_numeric_trace_and_remove_request_context() -> None:
    expression: Final = "p * 2 + c * 6"
    trace: Final = {
        "version": "expression-v1", "expression": expression,
        "expression_hash": hashlib.sha256(expression.encode()).hexdigest(),
        "timezone": "UTC", "started_at": "2026-01-01T00:00:00+00:00", "tiers": ["weekday"],
        "variables": {"p": "100", "c": "10", "api_key": "private"}, "usage": {"images": "4"},
        "undiscounted_amount": "0.00026", "discount": "0.7", "amount": "0.000182",
        "params": {"api_key": "private"}, "context": {"messages": ["private"]},
    }
    details: Final = _public_ledger_details({"expression_pricing": trace})
    assert details == {"expression_pricing": {
        key: value for key, value in {**trace, "variables": {"p": "100", "c": "10"}}.items()
        if key not in {"params", "context"}
    }}
    assert "private" not in str(details)
    assert _public_ledger_details({"expression_pricing": {**trace, "expression_hash": "0" * 64}}) == {}


@pytest.mark.parametrize("field", ("discount", "multiplier"))
def test_plan_metadata_accepts_an_explicit_neutral_legacy_discount_field(field: str) -> None:
    data: Final = PlanRequest.model_validate({"id": "shared", "name": "Shared", field: "1"})
    assert data.discount == "1"


@pytest.mark.parametrize("field", ("discount", "multiplier"))
def test_new_whole_plan_discounts_are_rejected_in_favor_of_selected_models(field: str) -> None:
    with pytest.raises(ValidationError, match="model-discounts"):
        PlanRequest.model_validate({"id": "shared", "name": "Shared", field: "0.7"})


@pytest.mark.parametrize("discount", ("0.7", "1"))
def test_new_whole_customer_discounts_are_rejected_but_selected_model_discounts_remain_available(discount: str) -> None:
    with pytest.raises(ValidationError, match="model-discounts"):
        SetDiscountRequest(discount=discount)
    assert SetModelDiscountRequest(models=("model-a",), discount=discount).discount == discount


def test_plan_metadata_edit_can_omit_the_legacy_global_discount() -> None:
    assert PlanRequest(id="shared", name="Renamed").discount is None


@pytest.mark.parametrize("models", ([], [""], [" "], ["same", "same"], ["a" * 192]))
def test_model_discount_selection_rejects_empty_duplicate_or_oversized_names(models: list[str]) -> None:
    with pytest.raises(ValidationError):
        SetModelDiscountRequest(models=models, discount="0.7")


def test_inactive_stored_model_rules_can_be_removed_but_cannot_receive_new_discounts() -> None:
    current: Final = {"items": [{"model": "inactive", "customer_discount": "0.7"}]}
    clearing: Final = SetModelDiscountRequest(models=("inactive",), discount=None)
    assert _validate_discount_selection(clearing, ("configured",), current) is None
    applying: Final = SetModelDiscountRequest(models=("configured", "inactive"), discount="0.8")
    with pytest.raises(HTTPException) as failure:
        _validate_discount_selection(applying, ("configured",), current)
    assert failure.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ("POST", "PUT", "PATCH", "DELETE"))
async def test_customer_model_discount_endpoint_has_no_write_methods(method: str) -> None:
    app: Final = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retail.test") as client:
        response: Final = await client.request(method, "/retail/model-discounts")
    assert response.status_code == 405


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/retail/admin/discount-models"),
        ("GET", "/retail/admin/wallets/other/model-discounts"),
        ("PUT", "/retail/admin/wallets/other/model-discounts"),
        ("GET", "/retail/admin/plans/shared/model-discounts"),
        ("PUT", "/retail/admin/plans/shared/model-discounts"),
    ),
)
async def test_customers_cannot_read_or_edit_administrator_model_discount_routes(method: str, path: str) -> None:
    async def authenticated_user() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="reader", user_role=LitellmUserRoles.INTERNAL_USER)

    app: Final = FastAPI()
    app.include_router(router)
    app.dependency_overrides[user_api_key_auth] = authenticated_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retail.test") as client:
        response: Final = await client.request(method, path, json={"models": ["model-a"], "discount": "0.7"})
    assert response.status_code == 403


@pytest.mark.parametrize("value", ("0", "-0.1", "1.01", "NaN", "Infinity", "0.1234567891", "1e-99999999"))
def test_plan_and_customer_discount_reject_markup_or_unrepresentable_values(value: str) -> None:
    with pytest.raises(ValidationError):
        PlanRequest(id="shared", name="Shared", discount=value)
    with pytest.raises(ValidationError):
        SetDiscountRequest(discount=value)


def test_an_explicit_null_clears_the_customer_override_but_an_omitted_field_does_not() -> None:
    assert SetDiscountRequest(discount=None).discount is None
    with pytest.raises(ValidationError):
        SetDiscountRequest.model_validate({})


def test_one_public_model_cannot_import_two_different_catalog_references() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ReferenceImportRequest.model_validate(
            {
                "items": [
                    {"model": "public", "catalog_model": "model-a", "source_label": "v1"},
                    {"model": "public", "catalog_model": "model-b", "source_label": "v1"},
                ]
            }
        )


def test_reference_import_rejects_unconfigured_models_and_stale_previews_before_writes() -> None:
    index: Final = {("public", "model-a"): {"model": "public", "catalog_model": "model-a", "source_label": "v2"}}
    stale: Final = ReferenceImportRequest.model_validate(
        {
            "items": [
                {"model": "public", "catalog_model": "model-a", "source_label": "v1"},
            ]
        }
    )
    with pytest.raises(HTTPException) as conflict:
        _select_references(stale, index)
    assert conflict.value.status_code == 409
    unknown: Final = ReferenceImportRequest.model_validate(
        {
            "items": [
                {"model": "other", "catalog_model": "model-a", "source_label": "v2"},
            ]
        }
    )
    with pytest.raises(HTTPException) as invalid:
        _select_references(unknown, index)
    assert invalid.value.status_code == 400
    valid: Final = ReferenceImportRequest.model_validate(
        {
            "items": [
                {"model": "public", "catalog_model": "model-a", "source_label": "v2"},
            ]
        }
    )
    assert _select_references(valid, index) == (index[("public", "model-a")],)


@pytest.mark.parametrize("fields", ({}, {"source_label": ""}, {"source_label": None}))
def test_reference_import_requires_the_reviewed_catalog_version(fields: dict[str, str | None]) -> None:
    with pytest.raises(ValidationError, match="source_label"):
        ReferenceImportRequest.model_validate({"items": [{"model": "public", "catalog_model": "model-a", **fields}]})


@pytest.mark.parametrize("field", ("user_id", "user_role", "role", "max_budget", "balance", "plan_id"))
def test_signup_rejects_client_privilege_and_funding_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        SignupRequest.model_validate(
            {"email": "reader@example.com", "password": "a strong test password", field: "proxy_admin"}
        )


@pytest.mark.parametrize("length", (0, 11, 129))
def test_signup_rejects_passwords_outside_supported_length(length: int) -> None:
    with pytest.raises(ValidationError):
        SignupRequest(email="reader@example.com", password="x" * length)


@pytest.mark.parametrize("length", (12, 128))
def test_signup_preserves_the_password_and_hides_it_in_model_representation(length: int) -> None:
    password: Final = "x" * length
    signup: Final = SignupRequest(email="reader@example.com", password=password)
    assert signup.password.get_secret_value() == password
    assert password not in repr(signup)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    (LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.INTERNAL_USER_VIEW_ONLY, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY),
)
@pytest.mark.parametrize("path", ("/retail/admin/wallets", "/retail/admin/pricing-catalog"))
async def test_non_administrators_cannot_access_administrator_routes(role: LitellmUserRoles, path: str) -> None:
    async def authenticated_user() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="reader", user_role=role)

    app: Final = FastAPI()
    app.include_router(router)
    app.dependency_overrides[user_api_key_auth] = authenticated_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retail.test") as client:
        response: Final = await client.get(path)
    assert response.status_code == 403
    assert "administrator" in response.json()["detail"]


@pytest.mark.asyncio
async def test_proxy_administrator_dependency_retains_the_native_principal() -> None:
    auth: Final = UserAPIKeyAuth(user_id="administrator", user_role=LitellmUserRoles.PROXY_ADMIN)
    assert await retail_admin(auth) is auth


@pytest.mark.asyncio
async def test_a_key_without_an_account_cannot_read_a_customer_wallet() -> None:
    with pytest.raises(HTTPException) as failure:
        await retail_user(UserAPIKeyAuth(user_role=LitellmUserRoles.INTERNAL_USER))
    assert failure.value.status_code == 403


def test_customer_ledger_removes_internal_payloads_and_costs() -> None:
    page: Final = _PublicLedgerPage.model_validate(
        {
            "items": [
                {
                    "id": "entry-1",
                    "kind": "charge",
                    "amount": "-0.200000000",
                    "balance_after": "0.800000000",
                    "reference": "",
                    "description": "API usage",
                    "request_id": "request-1",
                    "model": "customer-model",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "payload": {"provider_cost": "0.100000000"},
                    "details": {
                        "provider_cost": "0.100000000",
                        "upstream_secret": "must-not-leak",
                        "usage": {"input_tokens": 20, "output_tokens": 10, "raw_body": "must-not-leak"},
                        "price_snapshot": {
                            "model": "customer-model",
                            "version": "flat-token-v1",
                            "multiplier": "2",
                            "input_cost_per_token": "0.000005",
                            "provider_cost": "must-not-leak",
                        },
                        "billing_basis": "reported_usage",
                        "gateway_call_id": "call-1",
                    },
                    "actor_id": "operator-1",
                }
            ],
            "total": 1,
            "limit": 25,
            "offset": 0,
        }
    )
    body: Final = page.model_dump(mode="json")
    assert body["items"][0]["amount"] == "-0.200000000"
    assert body["items"][0]["model"] == "customer-model"
    assert body["items"][0]["details"]["usage"] == {"input_tokens": 20, "output_tokens": 10}
    assert body["items"][0]["details"]["price_snapshot"]["input_cost_per_token"] == "0.000005"
    assert body["items"][0]["details"]["billing_basis"] == "reported_usage"
    assert "payload" not in body["items"][0]
    assert "actor_id" not in body["items"][0]
    assert "must-not-leak" not in str(body)


@pytest.mark.parametrize("price", ("NaN", "Infinity", "-1", "1000000000001", "0.0000000000001", "1e-99999999"))
def test_model_sale_price_rejects_negative_nonfinite_oversized_or_unrepresentable_prices(price: str) -> None:
    with pytest.raises(ValueError, match="Price"):
        _per_token_price(price)


@pytest.mark.parametrize(
    ("price", "expected"),
    (("5", "0.000005"), ("0", "0"), ("0e99999999", "0"), ("0.000000000001", "0.000000000000000001")),
)
def test_model_sale_price_converts_per_million_without_floats(price: str, expected: str) -> None:
    assert _per_token_price(price) == expected


@pytest.mark.parametrize(
    "payload",
    (
        {"action": "settle"},
        {"action": "release", "amount": "0"},
        {"action": "refund"},
        {"action": "settle", "amount": "1", "user_id": "other-user"},
    ),
)
def test_manual_resolution_requires_an_explicit_unambiguous_operation(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ResolveRequest.model_validate({"reference": "ticket-1", "reason": "Verified upstream result", **payload})


def test_explicit_zero_dollar_settlement_is_allowed() -> None:
    resolution: Final = ResolveRequest(action="settle", amount="0", reference="ticket-1", reason="No billable usage")
    assert resolution.amount == "0"


def test_registration_rate_key_ignores_untrusted_forwarded_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_RATE_LIMIT_SECRET", "test-only-source-key")
    direct: Final = Request({"type": "http", "client": ("192.0.2.10", 1000), "headers": []})
    forged: Final = Request(
        {
            "type": "http",
            "client": ("192.0.2.10", 1000),
            "headers": [(b"x-forwarded-for", b"198.51.100.99")],
        }
    )
    assert _source_hash(direct) == _source_hash(forged)
    assert "192.0.2.10" not in _source_hash(direct)


@pytest.mark.asyncio
async def test_public_config_reports_disabled_registration_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_REGISTRATION_ENABLED", "false")
    app: Final = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retail.test") as client:
        response: Final = await client.get("/retail/config")
    assert response.status_code == 200
    assert response.json()["registration_enabled"] is False
    assert response.json()["login_url"] == "/ui/login/"
