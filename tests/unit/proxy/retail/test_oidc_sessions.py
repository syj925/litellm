from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.retail.oidc_sessions import (
    OIDC_SESSION_PREFIX,
    OIDC_SESSION_ROUTES,
    BindingSpec,
    SessionSettings,
    fresh_oidc_session,
    oidc_control_plane_route,
    oidc_request_key,
    reject_oidc_key_regeneration,
    reject_oidc_key_update,
    remember_oidc_credential,
)


class UnreachableNativeReader:
    async def query_raw(self, query: str, *args: object) -> object:
        raise AssertionError("Ordinary credentials must not read OIDC authority")


@dataclass(frozen=True, slots=True)
class NativeRoleAuthority:
    role: str

    async def query_raw(self, query: str, *args: object) -> object:
        return ({
            "user_id": "fixture-user", "user_role": self.role, "user_email": None,
            "password_reset_required": False, "expires_at": datetime(2050, 1, 1, tzinfo=timezone.utc),
            "session_id": "00000000-0000-0000-0000-000000000001",
            "metadata": {"login_method": "retail_oidc"}, "allowed_routes": OIDC_SESSION_ROUTES,
        },)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["proxy_admin_viewer", "internal_user_viewer", "customer"])
async def test_native_authority_role_replaces_stale_admin_from_the_key_cache(role: str) -> None:
    cached: Final = UserAPIKeyAuth(user_id="fixture-user", user_role="proxy_admin")
    verified: Final = await fresh_oidc_session(
        cached, OIDC_SESSION_PREFIX + "fixture", "/key/list", "GET", reader=NativeRoleAuthority(role),
    )
    assert verified.user_role == role
    assert cached.user_role == "proxy_admin"
    assert verified.metadata == {"login_method": "retail_oidc"}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/chat/completions", "/v1/messages", "/retail/wallet", "/key/list"])
async def test_ordinary_keys_preserve_identity_without_native_oidc_io(route: str) -> None:
    auth: Final = UserAPIKeyAuth(user_id="fixture-user", metadata={"label": "ordinary"})
    assert await fresh_oidc_session(
        auth, "sk-ordinary-fixture", route, "POST", reader=UnreachableNativeReader(),
    ) is auth


@pytest.mark.parametrize("route", [
    "/chat/completions", "/v1/chat/completions", "/v1/messages", "/embeddings", "/responses",
    "/v1beta/models/sample:generateContent", "/realtime", "/mcp", "/v1/mcp/server",
    "/v1/mcp/server/fixture/authorize", "/mcp-rest/tools/call", "/mcp-rest/tools/list",
    "/byok/authorize", "/experimental/tool",
    "/retail/auth/oidc/callback", "/unknown/passthrough", "/a2a/fixture",
])
def test_oidc_sessions_reject_inference_and_unrecognized_routes(route: str) -> None:
    assert oidc_control_plane_route(route, "POST") is False


@pytest.mark.parametrize("route", [
    "/key/list", "/key/generate", "/session/logout", "/retail/wallet", "/retail/organizations/mcp/members",
])
def test_oidc_route_scope_allows_control_plane_before_native_acl(route: str) -> None:
    assert oidc_control_plane_route(route, "POST") is True


@pytest.mark.asyncio
async def test_oidc_inference_denial_never_contacts_native_authority() -> None:
    with pytest.raises(HTTPException) as error:
        await fresh_oidc_session(
            UserAPIKeyAuth(user_id="fixture-user"), OIDC_SESSION_PREFIX + "fixture",
            "/v1/chat/completions", "POST", reader=UnreachableNativeReader(),
        )
    assert error.value.status_code == 403


def test_custom_header_credential_is_remembered_without_affecting_ordinary_keys() -> None:
    request: Final = Request({"type": "http", "method": "GET", "path": "/key/list", "headers": []})
    remember_oidc_credential(request, "sk-ordinary", "/key/list")
    assert oidc_request_key(request, "fallback") == "fallback"
    remember_oidc_credential(request, OIDC_SESSION_PREFIX + "fixture", "/key/list")
    assert oidc_request_key(request, "fallback") == OIDC_SESSION_PREFIX + "fixture"


@pytest.mark.parametrize("field", ["metadata", "user_id", "team_id", "allowed_routes", "models", "duration"])
def test_oidc_session_key_updates_cannot_change_identity_or_scope(field: str) -> None:
    with pytest.raises(HTTPException) as error:
        reject_oidc_key_update({"login_method": "retail_oidc"}, frozenset({"key", field}))
    assert error.value.status_code == 403
    reject_oidc_key_update({"login_method": "retail_oidc"}, frozenset({"key", "blocked"}))
    reject_oidc_key_update({"login_method": "username_password"}, frozenset({field}))


@pytest.mark.parametrize("metadata,key", [
    ({"login_method": "retail_oidc"}, "hashed-fixture-key"),
    ({}, OIDC_SESSION_PREFIX + "fixture-key"),
])
def test_oidc_key_regeneration_rejects_marker_and_reserved_raw_prefix(metadata: object, key: str) -> None:
    with pytest.raises(HTTPException) as error:
        reject_oidc_key_regeneration(metadata, key)
    assert error.value.status_code == 403


@pytest.mark.parametrize("enabled", ["false", "true", 0, 1, None])
def test_binding_does_not_coerce_non_boolean_enablement(enabled: object) -> None:
    with pytest.raises(ValidationError):
        BindingSpec.model_validate({"subject": "fixture-subject", "user_id": "fixture-user", "enabled": enabled})


def test_session_settings_reject_ambiguous_subject_bindings_without_exposing_subject() -> None:
    binding: Final = BindingSpec(subject="private-subject-fixture", user_id="private-user-fixture")
    with pytest.raises(ValidationError) as error:
        SessionSettings(
            namespace="fixture", generation=1, digest="a" * 64, issuer="https://fixture.invalid",
            session_seconds=300, bindings=(binding, binding),
        )
    assert "private-subject-fixture" not in str(error.value)
    assert "private-user-fixture" not in repr(binding)
