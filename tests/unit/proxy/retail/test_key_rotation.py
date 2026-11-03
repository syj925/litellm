from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from litellm.proxy._types import LiteLLM_VerificationToken, LitellmUserRoles, RegenerateKeyRequest, UserAPIKeyAuth
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.management_endpoints.key_management_endpoints import (
    can_modify_verification_token,
    prepare_key_update_data,
)
from litellm.proxy.retail.control_plane_access import NativeCredentialState, NativeUserState, native_master_actor
from litellm.proxy.retail.key_rotation import (
    RotateKeyRequest,
    rotation_target,
    rotation_user,
    writer_transaction_client,
)
from litellm.proxy.utils import PrismaClient, hash_token_if_needed
from litellm.repositories.verification_token_repository import VerificationTokenRepository


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
async def test_immediate_rotation_request_leaves_existing_native_key_settings_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


from litellm.constants import LITELLM_PROXY_MASTER_KEY_ALIAS


@dataclass(frozen=True, slots=True)
class IdentityAuthority:
    role: str = "internal_user"
    blocked: bool = False
    password_reset: bool = False

    async def user(self, user_id: str) -> NativeUserState:
        return NativeUserState(user_id=user_id, user_role=self.role, password_reset_required=self.password_reset)

    async def users(self, user_ids: tuple[str, ...]) -> tuple[NativeUserState, ...]:
        return tuple([await self.user(user_id) for user_id in user_ids])

    async def credential(self, token: str) -> NativeCredentialState:
        return NativeCredentialState(user_id="owner", blocked=self.blocked)


@pytest.mark.asyncio
async def test_rotation_rechecks_native_role_without_mutating_cached_auth() -> None:
    cached: Final = UserAPIKeyAuth(user_id="owner", api_key="fixture", user_role=LitellmUserRoles.PROXY_ADMIN)
    current: Final = await rotation_user(
        cached, "fixture-master", reader=IdentityAuthority(), now=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert current.user_role == LitellmUserRoles.INTERNAL_USER
    assert cached.user_role == LitellmUserRoles.PROXY_ADMIN
    assert current.api_key == cached.api_key and current.user_id == cached.user_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority",
    (
        IdentityAuthority(blocked=True),
        IdentityAuthority(password_reset=True),
        IdentityAuthority(role="internal_user_viewer"),
    ),
)
async def test_rotation_rejects_revoked_or_fresh_read_only_database_authority(authority: IdentityAuthority) -> None:
    cached: Final = UserAPIKeyAuth(user_id="owner", api_key="fixture", user_role=LitellmUserRoles.PROXY_ADMIN)
    with pytest.raises(HTTPException) as denied:
        await rotation_user(cached, "fixture-master", reader=authority, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert denied.value.status_code == 403


def test_native_master_exception_requires_server_owned_marker_and_admin_alias() -> None:
    claimed: Final = UserAPIKeyAuth(
        user_id="owner",
        api_key=LITELLM_PROXY_MASTER_KEY_ALIAS,
        user_role=LitellmUserRoles.PROXY_ADMIN,
        via_virtual_key=True,
    )
    assert claimed.via_virtual_key is False
    assert native_master_actor(claimed, True) is False
    claimed.via_virtual_key = True
    assert native_master_actor(claimed, True) is True
    assert native_master_actor(claimed, False) is False
    claimed.authenticated_by_custom_auth = True
    assert native_master_actor(claimed, True) is False


@pytest.mark.asyncio
async def test_native_verified_master_keeps_existing_non_database_rotation_path() -> None:
    master: Final = UserAPIKeyAuth(
        user_id="admin", api_key=LITELLM_PROXY_MASTER_KEY_ALIAS, user_role=LitellmUserRoles.PROXY_ADMIN
    )
    master.via_virtual_key = True
    assert await rotation_user(master, "fixture-master", reader=IdentityAuthority(blocked=True)) is master


@dataclass(frozen=True, slots=True)
class NativeTokenTable:
    owner: str

    async def find_unique(self, where: Mapping[str, str]) -> Mapping[str, str]:
        return {"token": where["token"], "user_id": self.owner}


@dataclass(frozen=True, slots=True)
class NativeTransaction:
    litellm_verificationtoken: NativeTokenTable

    def is_connected(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_rotation_uses_writer_transaction_owner_instead_of_stale_replica_owner() -> None:
    stale_reader: Final = NativeTransaction(NativeTokenTable("former-owner"))
    writer_transaction: Final = NativeTransaction(NativeTokenTable("current-owner"))
    client: Final = PrismaClient.__new__(PrismaClient)
    client.db = stale_reader
    bound: Final = writer_transaction_client(client, writer_transaction)
    old: Final = await VerificationTokenRepository(client).find_by_id("target")
    live: Final = await VerificationTokenRepository(bound).find_by_id("target")
    assert old is not None and live is not None
    caller: Final = UserAPIKeyAuth(user_id="former-owner", user_role=LitellmUserRoles.INTERNAL_USER)
    cache: Final = UserApiKeyCache()
    assert await can_modify_verification_token(old, cache, caller, client) is True
    assert await can_modify_verification_token(live, cache, caller, bound) is False
    assert (old.user_id, live.user_id) == ("former-owner", "current-owner")
