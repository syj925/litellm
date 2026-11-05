import asyncio
import importlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Final
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException, Request

import litellm
from litellm.proxy import proxy_server
from litellm.proxy._types import LiteLLM_UserTable, ProxyErrorTypes, ProxyException, UserAPIKeyAuth
from litellm.proxy.auth.auth_exception_handler import UserAPIKeyAuthExceptionHandler
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.retail.api_jwt_auth import API_JWT_SCOPE_KEY, ApiJwtAttempt, ApiJwtContext

_AUTH: Final = importlib.import_module("litellm.proxy.auth.user_api_key_auth")
_HASH: Final = "a" * 64
_PRIVATE: Final = "fixture-private-jwt-subject-jti"


def request(context: ApiJwtContext | ApiJwtAttempt) -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": [],
        "query_string": b"", API_JWT_SCOPE_KEY: context,
    })


def native_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "prisma_client": None, "user_api_key_cache": UserApiKeyCache(), "llm_router": None,
        "general_settings": {"disable_budget_reservation": True}, "user_custom_auth": None,
        "master_key": "sk-fixture-master", "litellm_proxy_admin_name": "fixture-native-admin",
        "proxy_logging_obj": MagicMock(post_call_failure_hook=AsyncMock(return_value=None), budget_alerts=AsyncMock()),
        "llm_model_list": [], "model_max_budget_limiter": MagicMock(),
    }.items():
        monkeypatch.setattr(proxy_server, name, value)
    monkeypatch.setattr(litellm, "max_budget", 0)


@pytest.mark.asyncio
async def test_shared_native_validator_uses_fresh_customer_instead_of_cached_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    native_globals(monkeypatch)
    owner: Final = LiteLLM_UserTable(user_id="bound-user", user_role="customer", rpm_limit=17, tpm_limit=29)
    stale: Final = AsyncMock(return_value=LiteLLM_UserTable(user_id=owner.user_id, user_role="proxy_admin"))
    monkeypatch.setattr(_AUTH, "get_user_object", stale)
    auth: Final = UserAPIKeyAuth(api_key=_HASH, token=_HASH, user_id=owner.user_id, user_role=owner.user_role)
    admitted: Final = await _AUTH.validate_resolved_virtual_key(
        valid_token=auth, api_key=_HASH, request=request(ApiJwtContext(owner, _HASH)), request_data={},
        route="/v1/chat/completions", parent_otel_span=None, start_time=datetime.now(timezone.utc),
        end_user_id=None, end_user_params={}, _end_user_object=None, fresh_user=owner,
    )
    await asyncio.sleep(0)
    assert admitted.user_role == "customer" and admitted.user_rpm_limit == 17 and admitted.user_tpm_limit == 29
    assert admitted.api_key == _HASH and admitted.token == _HASH and admitted.via_virtual_key is True
    stale.assert_not_awaited()


@pytest.mark.asyncio
async def test_common_checks_enforce_fresh_owner_budget_despite_cached_unlimited_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    native_globals(monkeypatch)
    owner: Final = LiteLLM_UserTable(user_id="bound-user", user_role="customer", max_budget=0.1, spend=1)
    stale: Final = AsyncMock(return_value=LiteLLM_UserTable(user_id=owner.user_id, user_role="proxy_admin"))
    monkeypatch.setattr(_AUTH, "get_user_object", stale)
    auth: Final = UserAPIKeyAuth(api_key=_HASH, token=_HASH, user_id=owner.user_id, user_role=owner.user_role)
    with pytest.raises(litellm.BudgetExceededError) as failure:
        await _AUTH.run_centralized_common_checks(
            user_api_key_auth_obj=auth, request=request(ApiJwtContext(owner, _HASH)),
            request_data={"model": "allowed-model"}, route="/v1/chat/completions", force_virtual_key_checks=True,
        )
    stale.assert_not_awaited()
    assert failure.value.current_cost == 1 and failure.value.max_budget == 0.1


@pytest.mark.asyncio
async def test_outer_wrapper_forces_budget_checks_even_when_native_route_would_skip_them(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.proxy.retail import api_jwt_auth

    native_globals(monkeypatch)
    owner: Final = LiteLLM_UserTable(user_id="bound-user", user_role="customer", max_budget=0.1, spend=1)
    auth: Final = UserAPIKeyAuth(api_key=_HASH, token=_HASH, user_id=owner.user_id, user_role=owner.user_role)
    monkeypatch.setattr(_AUTH, "user_api_key_auth_builder", AsyncMock(return_value=auth))
    monkeypatch.setattr(api_jwt_auth, "api_jwt_compatible", lambda: True)
    monkeypatch.setattr(api_jwt_auth, "coordination_guard_status", lambda: SimpleNamespace(accepting_credentials=True))
    monkeypatch.setattr(proxy_server, "general_settings", {
        "disable_budget_reservation": True,
        "pass_through_endpoints": [{"path": "/v1/chat/completions", "auth": False}],
    })
    selected: Final = request(ApiJwtContext(owner, _HASH))
    selected._body = json.dumps({"model": "allowed-model"}).encode()
    with pytest.raises(ProxyException) as failure:
        await _AUTH.user_api_key_auth(selected, None, None, None, None, None, None)
    assert failure.value.code == "401"
    assert "API access token authentication failed" in failure.value.message
    assert _HASH not in str(failure.value.to_dict())


class FailureTelemetry:
    def __init__(self) -> None:
        self.error = ""
        self.key: str | None = None
        self.token: str | None = None
        self.data: Mapping[str, object] = {}

    async def post_call_failure_hook(
        self, *, request_data: Mapping[str, object], original_exception: Exception,
        user_api_key_dict: UserAPIKeyAuth, error_type: ProxyErrorTypes, route: str,
    ) -> None:
        self.error = str(original_exception)
        self.key = user_api_key_dict.api_key
        self.token = user_api_key_dict.token
        self.data = request_data
        raise HTTPException(403, _PRIVATE + _HASH)


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", (False, True))
async def test_native_auth_failure_has_no_private_identity_and_cannot_fail_open(
    unavailable: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    sink: Final = FailureTelemetry()
    monkeypatch.setattr(proxy_server, "proxy_logging_obj", sink)
    monkeypatch.setattr(proxy_server, "general_settings", {"allow_requests_on_db_unavailable": True})
    monkeypatch.setattr(_AUTH.verbose_proxy_logger, "propagate", True)
    original: Final = httpx.ConnectError(_PRIVATE + _HASH) if unavailable else ProxyException(
        message=_PRIVATE + _HASH, type=ProxyErrorTypes.auth_error, param=_HASH, code=402,
    )
    with caplog.at_level(logging.WARNING, logger="LiteLLM Proxy"), pytest.raises(ProxyException) as failure:
        await UserAPIKeyAuthExceptionHandler.handle_authentication_error(
            original, request(ApiJwtAttempt()), {"private": _PRIVATE}, "/v1/chat/completions", None, None,
            resolved_identity=UserAPIKeyAuth(api_key=_HASH, token=_HASH, user_id=_PRIVATE),
        )
    assert failure.value.code == ("503" if unavailable else "402")
    assert sink.key is None and sink.token is None and sink.data == {}
    assert all(value not in str(failure.value.to_dict()) + sink.error + caplog.text for value in (_PRIVATE, _HASH))
    assert all(not record.exc_info for record in caplog.records)
    assert "Private credential authentication rejected" in caplog.text


def test_custom_header_debug_logging_never_prints_any_header_value(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: Final = Request({
        "type": "http", "method": "POST", "path": "/v1/chat/completions",
        "headers": [(b"x-custom-key", b"Bearer sk-fixture-private-value"), (b"x-other", _PRIVATE.encode())],
    })
    monkeypatch.setattr(_AUTH.verbose_proxy_logger, "propagate", True)
    with caplog.at_level(logging.DEBUG, logger="LiteLLM Proxy"):
        assert _AUTH.get_api_key_from_custom_header(selected, "x-custom-key") == "sk-fixture-private-value"
    assert "sk-fixture-private-value" not in caplog.text and _PRIVATE not in caplog.text
    assert "x-custom-key" in caplog.text


@pytest.mark.parametrize("mode", (
    "healthy", "missing-callback", "string-only-callback", "custom-auth", "custom-header",
    "native-jwt", "native-oauth", "replica", "missing-master", "missing-native",
))
def test_api_identity_requires_a_live_accounting_callback_and_compatible_native_mode(
    mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litellm.proxy.retail.api_jwt_auth import api_jwt_compatible
    from litellm.proxy.retail.billing import RetailBilling

    native_globals(monkeypatch)
    monkeypatch.delenv("DATABASE_URL_READ_REPLICA", raising=False)
    monkeypatch.setattr(proxy_server, "prisma_client", SimpleNamespace(db=object()))
    monkeypatch.setattr(litellm, "callbacks", [RetailBilling()])
    if mode == "missing-callback":
        monkeypatch.setattr(litellm, "callbacks", [])
    elif mode == "string-only-callback":
        monkeypatch.setattr(litellm, "callbacks", ["retail_billing"])
    elif mode == "custom-auth":
        monkeypatch.setattr(proxy_server, "user_custom_auth", AsyncMock())
    elif mode in {"custom-header", "native-jwt", "native-oauth"}:
        monkeypatch.setattr(proxy_server, "general_settings", {
            {"custom-header": "litellm_key_header_name", "native-jwt": "enable_jwt_auth", "native-oauth": "enable_oauth2_auth"}[mode]: True,
        })
    elif mode == "replica":
        monkeypatch.setenv("DATABASE_URL_READ_REPLICA", "fixture-replica")
    elif mode == "missing-master":
        monkeypatch.setattr(proxy_server, "master_key", None)
    elif mode == "missing-native":
        monkeypatch.setattr(proxy_server, "prisma_client", None)
    assert api_jwt_compatible() is (mode == "healthy")
