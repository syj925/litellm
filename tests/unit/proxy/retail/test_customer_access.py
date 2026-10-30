from typing import Final

import pytest
from fastapi import HTTPException

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import (
    GenerateKeyRequest,
    LitellmUserRoles,
    RegenerateKeyRequest,
    UpdateKeyRequest,
    UserAPIKeyAuth,
)
from litellm.proxy.retail.customer_access import (
    customer_key_generate_payload,
    customer_key_metadata,
    customer_resource_policy,
    safe_customer_key_info,
    safe_customer_model_group,
    safe_customer_model_info,
    validate_customer_key_request,
)


@pytest.fixture(autouse=True)
def enable_customer_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "true")


def _customer(**fields: object) -> UserAPIKeyAuth:
    return UserAPIKeyAuth(user_id="owner", user_role=LitellmUserRoles.INTERNAL_USER, **fields)


@pytest.mark.parametrize(
    "fields",
    (
        {"key_type": "default"}, {"key_type": "management"}, {"key_type": "read_only"},
        {"allowed_routes": []}, {"allowed_routes": ["management_routes"]},
        {"permissions": {"get_spend_routes": True}}, {"config": {"model_list": []}},
        {"router_settings": {"num_retries": 50}}, {"guardrails": []}, {"spend": 0},
        {"metadata": {"disable_global_guardrails": True}}, {"metadata": {"is_session_token": True}},
        {"team_id": UI_SESSION_TOKEN_TEAM_ID}, {"user_id": "another-owner"},
    ),
)
def test_customers_cannot_mint_management_keys_or_override_admin_configuration(fields: dict[str, object]) -> None:
    with pytest.raises(HTTPException) as failure:
        validate_customer_key_request(GenerateKeyRequest.model_validate(fields), _customer())
    assert failure.value.status_code == 403


@pytest.mark.parametrize("request_model", (UpdateKeyRequest, RegenerateKeyRequest))
def test_customer_key_update_and_rotation_reject_route_clearing_and_ui_session_mutation(
    request_model: type[UpdateKeyRequest] | type[RegenerateKeyRequest],
) -> None:
    with pytest.raises(HTTPException):
        validate_customer_key_request(
            request_model(key="owned", allowed_routes=[]), _customer(), existing_user_id="owner", updating=True,
        )
    with pytest.raises(HTTPException):
        validate_customer_key_request(
            request_model(key="owned"), _customer(), existing_team_id=UI_SESSION_TOKEN_TEAM_ID,
            existing_user_id="owner", updating=True,
        )


def test_default_customer_key_is_ai_only_with_account_resource_policy() -> None:
    request: Final = GenerateKeyRequest(key_alias="personal")
    validate_customer_key_request(request, _customer())
    actual: Final = customer_key_generate_payload(request.model_dump(exclude_unset=True), _customer(), None)
    assert actual == {
        "key_alias": "personal", "key_type": "llm_api", "allowed_routes": ["llm_api_routes"],
        "metadata": {"retail_resource_policy": {"mode": "auto"}},
    }


def test_customer_resource_policy_update_preserves_administrator_metadata() -> None:
    existing: Final = {"guardrails": ["required"], "retail_resource_policy": {"mode": "fixed", "pool_id": "first"}}
    updated: Final = customer_key_metadata({"retail_resource_policy": {"mode": "fixed", "pool_id": "second"}}, existing)
    assert updated == {"guardrails": ["required"], "retail_resource_policy": {"mode": "fixed", "pool_id": "second"}}
    assert customer_key_metadata(None, existing) == existing


def test_missing_customer_resource_policy_does_not_inherit_sitewide_legacy_access() -> None:
    assert customer_resource_policy(_customer()) == {"mode": "auto"}
    assert customer_resource_policy(_customer(team_id=UI_SESSION_TOKEN_TEAM_ID)) == {"mode": "auto"}
    assert customer_resource_policy(_customer(team_id="actual-team")) is None
    assert customer_resource_policy(UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN)) is None


def test_disabled_portal_preserves_native_key_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "false")
    request: Final = GenerateKeyRequest(allowed_routes=["management_routes"], metadata={"application": "native"})
    validate_customer_key_request(request, _customer())
    payload: Final = request.model_dump(exclude_unset=True)
    assert customer_key_generate_payload(payload, _customer(), None) == payload
    assert customer_resource_policy(_customer()) is None


def test_customer_model_metadata_returns_capabilities_without_supplier_routing_or_cost() -> None:
    row: Final = {
        "model_name": "public-model", "litellm_params": {"model": "vendor/private", "api_base": "https://private"},
        "model_info": {"id": "opaque-id", "supports_vision": True, "mode": "chat", "input_cost_per_token": 1},
        "internal_metadata": {"credential": "private-name"},
    }
    assert safe_customer_model_info((row,)) == [{
        "model_name": "public-model", "litellm_params": {"model": "public-model"},
        "model_info": {"id": "opaque-id", "supports_vision": True, "mode": "chat"},
    }]
    assert safe_customer_model_group({"model_group": "public-model", "providers": ["private"], "mode": "chat"}) == {
        "model_group": "public-model", "mode": "chat",
    }


def test_customer_key_readback_keeps_usable_limits_without_administrator_configuration_or_cost() -> None:
    policy: Final = {"mode": "fixed", "pool_id": "allowed-pool"}
    row: Final = {
        "token": "own-key-hash", "key_alias": "Application", "models": ["public-model"], "rpm_limit": 60,
        "spend": 1.5, "total_spend": 3.0, "model_max_budget_usage": {"public-model": {"spend": 1.5}},
        "config": {"api_base": "https://supplier.test"}, "router_settings": {"fallbacks": ["private-model"]},
        "allowed_routes": ["/private-route"], "permissions": {"get_spend_routes": True},
        "metadata": {"guardrails": ["admin-rule"], "retail_resource_policy": policy, "private": "operator data"},
        "user": {"user_id": "owner", "user_email": "owner@example.test", "metadata": {"private": "operator data"}},
    }
    assert safe_customer_key_info(row) == {
        "token": "own-key-hash", "key_alias": "Application", "models": ["public-model"], "rpm_limit": 60,
        "metadata": {"retail_resource_policy": policy},
        "user": {"user_id": "owner", "user_email": "owner@example.test"},
    }


@pytest.mark.parametrize("windows", ([{"max_budget": 10, "budget_duration": "1d", "current_spend": 2}],
                                     '[{"max_budget":10,"budget_duration":"1d","current_spend":2}]'))
def test_customer_key_budget_windows_keep_limits_without_native_cost(windows: object) -> None:
    assert safe_customer_key_info({"budget_limits": windows})["budget_limits"] == [
        {"max_budget": 10, "budget_duration": "1d"},
    ]
