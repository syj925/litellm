from typing import Final

import pytest
from fastapi import HTTPException
from pydantic import JsonValue

from litellm.proxy._types import (
    LiteLLM_VerificationToken, LitellmUserRoles, RegenerateKeyRequest, UpdateKeyRequest, UserAPIKeyAuth,
)
from litellm.proxy.management_endpoints.key_management_endpoints import prepare_key_update_data
from litellm.proxy.retail.key_policy import resource_policy_metadata, validate_native_key_resource_metadata


def test_unrelated_key_update_does_not_rewrite_existing_policy_or_metadata() -> None:
    existing: Final = {"retail_resource_policy": {"mode": "fixed", "pool_id": "pool-a"}, "application": "example"}
    assert resource_policy_metadata(None, existing, provided=False) is None


@pytest.mark.parametrize("replacement", ({}, None, {"application": "changed"}))
def test_metadata_replacement_cannot_remove_a_fixed_policy_and_reenable_legacy_routing(replacement: object) -> None:
    policy: Final = {"mode": "fixed", "pool_id": "pool-a"}
    expected: Final = {**(replacement if isinstance(replacement, dict) else {}), "retail_resource_policy": policy}
    assert resource_policy_metadata(replacement, {"retail_resource_policy": policy}) == expected


def test_owner_change_carries_policy_forward_for_validation_against_the_new_account() -> None:
    previous: Final = {"retail_resource_policy": {"mode": "custom", "grants": [{"pool_id": "pool-a", "models": ["alpha"]}]}}
    assert resource_policy_metadata(None, previous, provided=False, owner_changed=True) == previous


def test_explicit_automatic_policy_replaces_the_existing_fixed_policy() -> None:
    assert resource_policy_metadata(
        {"retail_resource_policy": {"mode": "auto"}},
        {"retail_resource_policy": {"mode": "fixed", "pool_id": "pool-a"}},
    ) == {"retail_resource_policy": {"mode": "auto"}}


@pytest.mark.asyncio
async def test_unbound_key_cannot_carry_an_account_resource_policy(monkeypatch) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "true")
    with pytest.raises(HTTPException) as error:
        await validate_native_key_resource_metadata({"retail_resource_policy": {"mode": "auto"}}, None)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_disabled_retail_portal_keeps_native_metadata_without_any_retail_database(monkeypatch) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "false")
    monkeypatch.delenv("RETAIL_DATABASE_URL", raising=False)
    assert await validate_native_key_resource_metadata({"application": "example"}, None) == {"application": "example"}
    with pytest.raises(HTTPException, match="retail portal is disabled"):
        await validate_native_key_resource_metadata({"retail_resource_policy": {"mode": "auto"}}, "customer")


@pytest.mark.asyncio
@pytest.mark.parametrize("request_model", (UpdateKeyRequest, RegenerateKeyRequest))
async def test_native_update_and_regenerate_preparation_cannot_remove_pool_policy_via_empty_metadata(
    monkeypatch, request_model: type[UpdateKeyRequest] | type[RegenerateKeyRequest],
) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "false")
    existing: Final = LiteLLM_VerificationToken(
        token="test-key-hash", user_id="customer",
        metadata={"retail_resource_policy": {"mode": "fixed", "pool_id": "pool-a"}},
    )
    request: Final = request_model(key="test-key-hash", metadata={})
    with pytest.raises(HTTPException, match="retail portal is disabled") as error:
        await prepare_key_update_data(request, existing)
    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("routes", ([], ["info_routes"], ["/v1/messages"]))
async def test_customer_key_update_keeps_admin_metadata_and_never_widens_restricted_routes(
    routes: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litellm.proxy.retail import key_policy

    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "true")
    retained: Final = {"guardrails": ["required"], "retail_resource_policy": {"mode": "auto"}}

    async def validate_policy(
        metadata: object, user_id: str | None, existing_metadata: object = None,
        *, provided: bool = True, owner_changed: bool = False,
    ) -> dict[str, JsonValue]:
        assert metadata == retained
        assert user_id == "owner"
        return retained

    monkeypatch.setattr(key_policy, "validate_native_key_resource_metadata", validate_policy)
    existing: Final = LiteLLM_VerificationToken(
        token="owned-key", user_id="owner", allowed_routes=routes, metadata=retained,
    )
    payload: Final = await prepare_key_update_data(
        UpdateKeyRequest(key="owned-key", metadata={"retail_resource_policy": {"mode": "auto"}}), existing,
        user_api_key_dict=UserAPIKeyAuth(user_id="owner", user_role=LitellmUserRoles.INTERNAL_USER),
    )
    assert payload["metadata"] == retained
    assert payload.get("allowed_routes", routes) == (routes or ["llm_api_routes"])
