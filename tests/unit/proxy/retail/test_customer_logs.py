import json
from typing import Final

import pytest

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail.customer_logs import (
    customer_activity_response,
    customer_log_detail,
    customer_log_response,
    customer_user_info_response,
    public_activity_data,
)


@pytest.fixture(autouse=True)
def enable_customer_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "true")


def _customer() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(user_id="owner", user_role=LitellmUserRoles.INTERNAL_USER)


@pytest.mark.parametrize("paged", (False, True))
def test_customer_logs_keep_public_usage_and_pool_without_supplier_cost_or_routing(paged: bool) -> None:
    row: Final = {
        "request_id": "request", "model": "vendor/private-model", "model_group": "public-model",
        "api_base": "https://private.example", "model_id": "private-deployment", "custom_llm_provider": "vendor",
        "spend": 3, "session_total_spend": 6, "mcp_tool_call_spend": 2,
        "total_tokens": 12, "status": "failure", "session_total_count": 2,
        "metadata": {
            "user_api_key_alias": "my-key", "retail_resource_pool_name": "Allowed pool",
            "retail_resource_pool_id": "allowed-pool", "provider_cost": 3,
            "error_information": {"error_message": "private upstream error body"},
            "api_base": "https://private.example", "router_metadata": {"credential": "private-credential"},
        },
    }
    payload: Final = {"data": [row], "total": 1} if paged else [row]
    actual: Final = customer_log_response(payload, _customer())
    rows: Final = actual["data"] if isinstance(actual, dict) else actual
    assert rows[0]["model"] == "public-model"
    assert rows[0]["total_tokens"] == 12
    assert rows[0]["metadata"]["retail_resource_pool_name"] == "Allowed pool"
    assert rows[0]["status"] == "failure"
    serialized: Final = json.dumps(actual)
    assert "private" not in serialized
    assert "spend" not in serialized
    assert "provider_cost" not in serialized


def test_customer_log_detail_keeps_own_conversation_without_private_headers_or_response_cost() -> None:
    messages: Final = [{"role": "user", "content": "hello"}]
    payload: Final = {
        "messages": messages,
        "response": json.dumps({
            "id": "response", "model": "vendor/private-model", "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "response_cost": 3},
            "_hidden_params": {"api_base": "https://private.example"},
        }),
        "proxy_server_request": {
            "url": "https://private.example", "headers": {"Authorization": "private-value"},
            "body": {"model": "public-model", "messages": messages, "api_key": "private-value"},
        },
    }
    actual: Final = customer_log_detail(payload, _customer())
    assert actual["messages"] is messages
    assert actual["proxy_server_request"]["body"]["model"] == "public-model"
    response: Final = json.loads(actual["response"])
    assert response["model"] == "public-model"
    assert response["choices"][0]["message"]["content"] == "hi"
    assert response["usage"] == {"prompt_tokens": 1, "completion_tokens": 2}
    assert "private" not in json.dumps(actual)


def test_customer_error_detail_does_not_return_raw_upstream_body() -> None:
    payload: Final = {"response": {"error": {"message": "private upstream HTML"}}, "messages": []}
    actual: Final = customer_log_detail(payload, _customer())
    assert "private" not in json.dumps(actual)
    assert actual["response"]["error"]["type"] == "api_error"


def test_customer_activity_preserves_counts_and_public_models_without_native_cost() -> None:
    payload: Final = {
        "metadata": {"total_spend": 3, "total_tokens": 10},
        "results": [{
            "metrics": {"spend": 3, "flat_cost": 1, "prompt_caching_savings_spend": 2, "api_requests": 1},
            "breakdown": {
                "models": {"private-model": {"metrics": {"spend": 3}}},
                "providers": {"private-provider": {"metrics": {"spend": 3}}},
                "model_groups": {"public-cost-model": {"metrics": {"total_tokens": 10, "spend": 3}}},
            },
        }],
    }
    actual: Final = public_activity_data(payload)
    assert actual["metadata"] == {"total_tokens": 10}
    assert actual["results"][0]["metrics"] == {"api_requests": 1}
    assert actual["results"][0]["breakdown"]["models"] == {
        "public-cost-model": {"metrics": {"total_tokens": 10}},
    }
    assert "private" not in json.dumps(actual)
    assert "spend" not in json.dumps(actual)
    response: Final = customer_activity_response(payload, _customer())
    assert json.loads(response.body) == actual


@pytest.mark.parametrize("role", (LitellmUserRoles.PROXY_ADMIN, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY))
def test_administrator_log_and_activity_responses_keep_original_identity(role: LitellmUserRoles) -> None:
    payload: Final = {"data": [{"spend": 3, "api_base": "https://private.example"}]}
    auth: Final = UserAPIKeyAuth(user_role=role)
    assert customer_log_response(payload, auth) is payload
    assert customer_log_detail(payload, auth) is payload
    assert customer_activity_response(payload, auth) is payload


def test_disabled_portal_preserves_native_log_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "false")
    payload: Final = {"spend": 3}
    assert customer_log_response(payload, _customer()) is payload


@pytest.mark.parametrize("wrapped", (False, True))
def test_customer_user_info_keeps_account_limits_and_team_admin_membership_without_operator_metadata(
    wrapped: bool,
) -> None:
    account: Final = {
        "user_id": "owner", "user_email": "owner@example.test", "user_role": "internal_user", "spend": 10,
        "max_budget": 100, "tpm_limit": 2000, "rpm_limit": 20, "models": ["public-model"],
        "model_max_budget_usage": {"public-model": {"spend": 1}},
        "metadata": {"theme": "dark", "password_reset_required": False, "logging": {"api_base": "private"}},
    }
    team: Final = {
        "team_id": "team", "team_alias": "My team", "spend": 10, "metadata": {"logging": "private"},
        "members_with_roles": [{"role": "admin", "user_id": "owner", "metadata": {"private": True}}],
    }
    payload: Final = {
        "user_id": "owner", "user_info": account, "teams": [team],
        "keys": [{"token": "own-key", "spend": 10, "config": {"api_base": "private"}}],
    } if wrapped else account
    result: Final = customer_user_info_response(payload, _customer())
    actual: Final = json.loads(result.body)
    user: Final = actual["user_info"] if wrapped else actual
    assert user["max_budget"] == 100
    assert user["tpm_limit"] == 2000
    assert user["rpm_limit"] == 20
    assert user["models"] == ["public-model"]
    assert user["metadata"] == {"theme": "dark", "password_reset_required": False}
    assert "spend" not in json.dumps(actual)
    assert "private" not in json.dumps(actual)
    if wrapped:
        assert actual["teams"][0]["members_with_roles"] == [{"role": "admin", "user_id": "owner"}]


def test_administrator_user_info_response_keeps_original_identity() -> None:
    payload: Final = {"user_id": "owner", "spend": 10, "metadata": {"logging": "operator"}}
    assert customer_user_info_response(payload, UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN)) is payload
