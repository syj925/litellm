from typing import Final

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from litellm.proxy._types import LiteLLM_VerificationToken, LitellmUserRoles, RegenerateKeyRequest, UserAPIKeyAuth
from litellm.proxy.management_endpoints.key_management_endpoints import prepare_key_update_data
from litellm.proxy.retail.key_rotation import RotateKeyRequest, rotation_target
from litellm.proxy.utils import hash_token_if_needed


@pytest.mark.parametrize(
    "field,value",
    (
        ("models", ["other"]),
        ("max_budget", 100),
        ("spend", 0),
        ("duration", "30d"),
        ("metadata", {}),
        ("grace_period", "1d"),
    ),
)
def test_rotation_request_rejects_permission_billing_and_expiry_changes(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        RotateKeyRequest.model_validate({"key": "key-id", field: value})


@pytest.mark.parametrize("hashed", (False, True))
def test_master_key_cannot_be_rotated_by_raw_secret_or_hash(hashed: bool) -> None:
    master: Final = "sk-test-master-key-that-is-not-a-real-secret"
    target: Final = hash_token_if_needed(master) if hashed else master
    with pytest.raises(HTTPException, match="Master key rotation") as error:
        rotation_target(target, master, UserAPIKeyAuth(user_id="admin", user_role=LitellmUserRoles.PROXY_ADMIN))
    assert error.value.status_code == 400


@pytest.mark.parametrize("role", (LitellmUserRoles.INTERNAL_USER_VIEW_ONLY, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY))
def test_read_only_accounts_cannot_rotate_even_their_own_key(role: LitellmUserRoles) -> None:
    with pytest.raises(HTTPException, match="Read-only accounts") as error:
        rotation_target("key-id", None, UserAPIKeyAuth(user_id="owner", user_role=role))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_immediate_rotation_request_leaves_existing_native_key_settings_unchanged(monkeypatch) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "false")
    existing: Final = LiteLLM_VerificationToken(
        token="key-id",
        user_id="owner",
        models=["alpha"],
        spend=8,
        max_budget=20,
        metadata={"application": "example"},
        tpm_limit=100,
        rpm_limit=10,
    )
    before: Final = existing.model_dump()
    changes: Final = await prepare_key_update_data(RegenerateKeyRequest(grace_period="0s"), existing)
    assert changes == {"metadata": before["metadata"]}
    assert existing.model_dump() == before
