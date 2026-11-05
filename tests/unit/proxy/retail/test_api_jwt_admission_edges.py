import json
from types import SimpleNamespace
from typing import Final
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from litellm.proxy import proxy_server
from litellm.proxy._types import LiteLLM_UserTable, ProxyException, UserAPIKeyAuth
from litellm.proxy.retail import api_jwt_auth
from litellm.proxy.retail.api_jwt_auth import ApiJwtContext
from tests.unit.proxy.retail.test_api_jwt_middleware import Probe
from tests.unit.proxy.retail.test_api_jwt_native_auth import _AUTH, native_globals, request

_WRAPPED: Final = "rt-jwt.fixture-private-access-jwt-subject-jti"


@pytest.mark.asyncio
async def test_ordinary_noncredential_header_can_contain_reserved_namespace_without_becoming_a_credential() -> None:
    probe: Final = Probe()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe.app()), base_url="https://gateway.test") as client:
        response: Final = await client.post("/v1/chat/completions", headers={
            "Authorization": "Bearer sk-normal", "User-Agent": "docs-example-rt-jwt.client", "X-Unrelated": "public-rt-jwt.documentation",
        }, json={})
    assert response.status_code == 200 and response.json()["ordinary_key"] == "Bearer sk-normal"
    assert response.json()["header"] == "public-rt-jwt.documentation" and not probe.private_input


@pytest.mark.asyncio
async def test_redundant_reserved_namespace_in_other_headers_is_not_forwarded_for_a_jwt_request() -> None:
    probe: Final = Probe()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe.app()), base_url="https://gateway.test") as client:
        response: Final = await client.post("/v1/chat/completions", headers={
            "Authorization": "Bearer " + _WRAPPED, "X-Unrelated": _WRAPPED,
        }, json={})
    assert response.status_code == 200 and response.json()["header"] is None and probe.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ("cookie", "custom-header", "mixed-custom-header"))
async def test_cookie_rejection_dynamic_header_capture_and_mixed_credential_rejection(
    source: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxy_server, "general_settings", {"litellm_key_header_name": "X-Custom-Key"})
    headers: Final = (
        {"Cookie": "token=" + _WRAPPED} if source == "cookie"
        else {"X-Custom-Key": _WRAPPED} if source == "custom-header"
        else {"Authorization": "Bearer " + _WRAPPED, "X-Custom-Key": "sk-other"}
    )
    probe: Final = Probe()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe.app()), base_url="https://gateway.test") as client:
        response: Final = await client.post("/v1/chat/completions", headers=headers, json={})
    if source == "custom-header":
        assert response.status_code == 200 and probe.private_input
    else:
        assert response.status_code == 401 and probe.calls == 0
    assert _WRAPPED not in response.text


@pytest.mark.asyncio
async def test_wrapper_checks_final_recovered_identity_again_before_returning(monkeypatch: pytest.MonkeyPatch) -> None:
    native_globals(monkeypatch)
    key_hash: Final = "a" * 64
    owner: Final = LiteLLM_UserTable(user_id="bound-user", user_role="customer")
    auth: Final = UserAPIKeyAuth(api_key=key_hash, token=key_hash, user_id=owner.user_id, user_role=owner.user_role)
    monkeypatch.setattr(_AUTH, "user_api_key_auth_builder", AsyncMock(return_value=auth))
    monkeypatch.setattr(_AUTH, "_authorize_authenticated_request", AsyncMock(return_value=auth.model_copy(update={"user_role": "proxy_admin"})))
    monkeypatch.setattr(api_jwt_auth, "api_jwt_compatible", lambda: True)
    monkeypatch.setattr(api_jwt_auth, "coordination_guard_status", lambda: SimpleNamespace(accepting_credentials=True))
    selected: Final = request(ApiJwtContext(owner, key_hash))
    selected._body = json.dumps({"model": "allowed-model"}).encode()
    with pytest.raises(HTTPException) as failure:
        await _AUTH.user_api_key_auth(selected, None, None, None, None, None, None)
    assert failure.value.status_code == 401 and key_hash not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ("azure", "anthropic", "google", "apim", "litellm", "configured"))
async def test_native_entry_without_sanitizer_never_delegates_alternate_reserved_credentials(
    source: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_globals(monkeypatch)
    custom: Final = AsyncMock(return_value=UserAPIKeyAuth(user_role="proxy_admin"))
    monkeypatch.setattr(proxy_server, "user_custom_auth", custom)
    selected: Final = request(ApiJwtContext(LiteLLM_UserTable(user_id="ignored"), "a" * 64))
    selected.scope.pop(api_jwt_auth.API_JWT_SCOPE_KEY)
    if source == "configured":
        monkeypatch.setattr(proxy_server, "general_settings", {"litellm_key_header_name": "X-Custom-Key"})
        selected.scope["headers"] = [(b"x-custom-key", _WRAPPED.encode())]
    with pytest.raises(ProxyException) as failure:
        await _AUTH.user_api_key_auth_builder(
            selected, None, _WRAPPED if source == "azure" else None,
            _WRAPPED if source == "anthropic" else None, _WRAPPED if source == "google" else None,
            _WRAPPED if source == "apim" else None, {}, _WRAPPED if source == "litellm" else None,
        )
    assert failure.value.code == "401" and _WRAPPED not in str(failure.value.to_dict())
    custom.assert_not_awaited()
