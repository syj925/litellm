from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

import pytest
from fastapi import HTTPException

from litellm.constants import LITELLM_PROXY_MASTER_KEY_ALIAS, UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail.control_plane_access import (
    NativeCredentialState,
    NativeUserState,
    active_target_users,
    native_master_actor,
    verify_actor,
    verify_target_user,
)


@dataclass(frozen=True, slots=True)
class Identity:
    account: NativeUserState | None
    token: NativeCredentialState | None
    unavailable: bool = False

    async def user(self, user_id: str) -> NativeUserState | None:
        if self.unavailable:
            raise ConnectionError("fixture authority unavailable")
        return self.account if self.account is not None and self.account.user_id == user_id else None

    async def credential(self, token: str) -> NativeCredentialState | None:
        return self.token

    async def users(self, user_ids: tuple[str, ...]) -> tuple[NativeUserState, ...]:
        if self.unavailable:
            raise ConnectionError("fixture authority unavailable")
        return (self.account,) if self.account is not None and self.account.user_id in user_ids else ()


NOW: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("native_role", "session_role", "is_admin", "can_write"),
    (
        ("proxy_admin", LitellmUserRoles.PROXY_ADMIN, True, True),
        ("internal_user", LitellmUserRoles.PROXY_ADMIN, False, True),
        ("proxy_admin", LitellmUserRoles.INTERNAL_USER, False, True),
        ("proxy_admin_viewer", LitellmUserRoles.PROXY_ADMIN, False, False),
        ("internal_user_viewer", LitellmUserRoles.INTERNAL_USER, False, False),
    ),
)
async def test_database_role_and_session_role_both_bound_administrator_permission(
    native_role: str, session_role: LitellmUserRoles, is_admin: bool, can_write: bool
) -> None:
    auth: Final = UserAPIKeyAuth(user_id="user", api_key="sk-unit-fixture", user_role=session_role)
    reader: Final = Identity(
        NativeUserState(user_id="user", user_role=native_role), NativeCredentialState(user_id="user")
    )
    actor: Final = await verify_actor(auth, reader=reader, now=NOW)
    assert (actor.user_id, actor.native_role, actor.is_admin, actor.can_write) == (
        "user",
        native_role,
        is_admin,
        can_write,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential",
    (
        None,
        NativeCredentialState(user_id="other"),
        NativeCredentialState(user_id="user", blocked=True),
        NativeCredentialState(user_id="user", expires=NOW),
        NativeCredentialState(user_id="user", expires=NOW - timedelta(seconds=1)),
    ),
)
async def test_missing_reassigned_blocked_or_expired_session_is_denied(
    credential: NativeCredentialState | None,
) -> None:
    reader: Final = Identity(NativeUserState(user_id="user", user_role="proxy_admin"), credential)
    with pytest.raises(HTTPException) as denied:
        await verify_actor(UserAPIKeyAuth(user_id="user", api_key="sk-unit-fixture"), reader=reader, now=NOW)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", (None, "unknown-role"))
async def test_missing_or_unknown_database_role_never_inherits_cached_admin(role: str | None) -> None:
    reader: Final = Identity(NativeUserState(user_id="user", user_role=role), NativeCredentialState(user_id="user"))
    with pytest.raises(HTTPException) as denied:
        await verify_actor(
            UserAPIKeyAuth(user_id="user", api_key="sk-unit-fixture", user_role=LitellmUserRoles.PROXY_ADMIN),
            reader=reader,
            now=NOW,
        )
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_authority_failure_fails_closed_without_exposing_database_error() -> None:
    with pytest.raises(HTTPException) as denied:
        await verify_actor(
            UserAPIKeyAuth(user_id="user", api_key="sk-unit-fixture"),
            reader=Identity(None, None, unavailable=True),
            now=NOW,
        )
    assert (denied.value.status_code, denied.value.detail) == (503, "Account authority is temporarily unavailable")


@pytest.mark.asyncio
async def test_reset_required_target_cannot_be_associated() -> None:
    reader: Final = Identity(
        NativeUserState(user_id="user", user_role="internal_user", password_reset_required=True), None
    )
    with pytest.raises(HTTPException) as denied:
        await verify_target_user("user", reader=reader)
    assert denied.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ("internal_user", "proxy_admin", "internal_user_viewer", "proxy_admin_viewer"))
async def test_writable_grant_target_check_preserves_readonly_association_behavior(role: str) -> None:
    reader: Final = Identity(NativeUserState(user_id="user", user_role=role), None)
    assert await verify_target_user("user", reader=reader) is None
    if role.endswith("_viewer"):
        with pytest.raises(HTTPException) as denied:
            await verify_target_user("user", reader=reader, writable=True)
        assert denied.value.status_code == 404
    else:
        assert await verify_target_user("user", reader=reader, writable=True) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", (LitellmUserRoles.ORG_ADMIN, LitellmUserRoles.TEAM))
async def test_optional_role_scope_rejects_unsupported_grants_without_restricting_existing_targets(
    role: LitellmUserRoles,
) -> None:
    reader: Final = Identity(NativeUserState(user_id="user", user_role=role.value), None)
    assert await verify_target_user("user", reader=reader, writable=True) is None
    with pytest.raises(HTTPException) as denied:
        await verify_target_user(
            "user", reader=reader, writable=True,
            allowed_roles=frozenset((LitellmUserRoles.INTERNAL_USER.value,)),
        )
    assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_fresh_allowed_routes_revoke_an_existing_session_route() -> None:
    reader: Final = Identity(
        NativeUserState(user_id="user", user_role="internal_user"),
        NativeCredentialState(user_id="user", allowed_routes=("/v1/chat/completions",)),
    )
    with pytest.raises(HTTPException) as denied:
        await verify_actor(
            UserAPIKeyAuth(user_id="user", api_key="sk-unit-fixture", request_route="/retail/organizations"),
            reader=reader,
            now=NOW,
        )
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_role", (LitellmUserRoles.PROXY_ADMIN, LitellmUserRoles.INTERNAL_USER))
async def test_route_scope_uses_persisted_role_and_team_instead_of_cached_admin_or_dashboard_claims(
    cached_role: LitellmUserRoles, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "true")
    reader: Final = Identity(
        NativeUserState(user_id="user", user_role="internal_user"),
        NativeCredentialState(user_id="user", team_id=None),
    )
    cached: Final = UserAPIKeyAuth(
        user_id="user",
        api_key="sk-unit-fixture",
        user_role=cached_role,
        team_id=UI_SESSION_TOKEN_TEAM_ID,
        request_route="/retail/reports",
    )
    with pytest.raises(HTTPException) as denied:
        await verify_actor(cached, reader=reader, now=NOW)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_live_dashboard_credential_keeps_management_route_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "true")
    reader: Final = Identity(
        NativeUserState(user_id="user", user_role="internal_user"),
        NativeCredentialState(user_id="user", team_id=UI_SESSION_TOKEN_TEAM_ID),
    )
    actor: Final = await verify_actor(
        UserAPIKeyAuth(user_id="user", api_key="sk-unit-fixture", request_route="/retail/reports"),
        reader=reader,
        now=NOW,
    )
    assert actor.user_id == "user" and actor.is_admin is False


@pytest.mark.asyncio
async def test_active_report_targets_exclude_missing_or_reset_accounts() -> None:
    reader: Final = Identity(NativeUserState(user_id="user", user_role="internal_user"), None)
    assert await active_target_users(("user", "missing"), reader=reader) == frozenset(("user",))
    reset: Final = Identity(
        NativeUserState(user_id="user", user_role="internal_user", password_reset_required=True), None
    )
    assert await active_target_users(("user",), reader=reset) == frozenset()
    with pytest.raises(HTTPException) as denied:
        await active_target_users(("user",), reader=Identity(None, None, unavailable=True))
    assert denied.value.status_code == 503


def _native_master() -> UserAPIKeyAuth:
    auth: Final = UserAPIKeyAuth(
        user_id="configured-admin-identity",
        api_key=LITELLM_PROXY_MASTER_KEY_ALIAS,
        user_role=LitellmUserRoles.PROXY_ADMIN,
    )
    auth.via_virtual_key = True
    return auth


@pytest.mark.asyncio
async def test_native_master_preserves_native_actor_identity_without_a_synthetic_database_account() -> None:
    auth: Final = _native_master()
    actor: Final = await verify_actor(
        auth, reader=Identity(None, None, unavailable=True), now=NOW, configured_master_present=True
    )
    assert (actor.user_id, actor.native_role, actor.is_admin, actor.can_write) == (
        auth.user_id,
        "proxy_admin",
        True,
        True,
    )
    assert native_master_actor(auth, configured_master_present=True) is True


@pytest.mark.asyncio
async def test_serialized_native_markers_are_stripped_and_cannot_forge_a_master() -> None:
    forged: Final = UserAPIKeyAuth.model_validate(
        {
            "user_id": "forged-admin",
            "api_key": LITELLM_PROXY_MASTER_KEY_ALIAS,
            "user_role": "proxy_admin",
            "via_virtual_key": True,
            "authenticated_by_custom_auth": False,
        }
    )
    assert forged.via_virtual_key is False and not native_master_actor(forged, configured_master_present=True)
    with pytest.raises(HTTPException) as denied:
        await verify_actor(forged, reader=Identity(None, None), now=NOW, configured_master_present=True)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ("no-configured-master", "custom-auth", "ordinary-role", "ordinary-key"))
async def test_master_access_requires_all_native_authentication_conditions(invalid: str) -> None:
    auth: Final = _native_master()
    if invalid == "custom-auth":
        auth.authenticated_by_custom_auth = True
    if invalid == "ordinary-role":
        auth.user_role = LitellmUserRoles.INTERNAL_USER
    if invalid == "ordinary-key":
        auth.api_key = "ordinary-key-token"
    configured: Final = invalid != "no-configured-master"
    assert not native_master_actor(auth, configured_master_present=configured)
    with pytest.raises(HTTPException) as denied:
        await verify_actor(auth, reader=Identity(None, None), now=NOW, configured_master_present=configured)
    assert denied.value.status_code == 403
