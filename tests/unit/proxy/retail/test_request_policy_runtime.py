from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Final
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import JsonValue

import litellm
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy import proxy_server
from litellm.proxy._types import LiteLLM_UserTable, ProxyException, UserAPIKeyAuth
from litellm.proxy.retail import request_policy_runtime as runtime
from litellm.proxy.retail.request_policy_types import (
    ApplicablePolicy,
    Definition,
    RequestPolicyFailure,
    ResolvedRequestPolicies,
)
from litellm.router_utils.managed_credential_errors import ManagedCredentialRequestRejected
from tests.unit.proxy.retail.test_api_jwt_native_auth import native_globals

_HASH: Final = "a" * 64
_TIME: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)
_AUTH: Final = UserAPIKeyAuth(api_key=_HASH, token=_HASH, user_id="fixture-user", user_role="customer", created_at=_TIME)
_USER: Final = LiteLLM_UserTable(user_id="fixture-user", user_role="customer", max_budget=7)
_DEPLOYMENT: Final = {"model_info": {"id": "fixture-deployment"}, "litellm_params": {"model": "openai/fixture-backend"}}


@dataclass(frozen=True, slots=True)
class Lookup:
    result: ResolvedRequestPolicies | RequestPolicyFailure

    async def __call__(self, auth: UserAPIKeyAuth, model: str) -> ResolvedRequestPolicies | RequestPolicyFailure:
        assert auth is _AUTH and model == "fixture-model"
        return self.result


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", "true")
    monkeypatch.setattr(runtime, "_require_environment", lambda: None)
    monkeypatch.setattr(proxy_server, "llm_router", SimpleNamespace(get_model_list=lambda model, team_id: (_DEPLOYMENT,)))


def request(path: str = "/v1/chat/completions") -> Request:
    return Request({"type": "http", "method": "POST", "path": path, "headers": []})


def body(changes: Mapping[str, JsonValue] | None = None) -> Mapping[str, object]:
    return {"model": "fixture-model", "messages": [{"role": "user", "content": "client input"}], **(changes or {})}


async def authorized(
    *, route: str = "/v1/chat/completions", prompt: str | None = None, data: Mapping[str, object] | None = None,
    definition: Definition | None = None,
) -> UserAPIKeyAuth:
    plan: Final = ApplicablePolicy("key", UUID(int=1), 1, definition or Definition(max_output_tokens=128, temperature=0.5), prompt)
    return await runtime.authorize_request_policies(request(route), _AUTH, data or body(), route,
        lookup=Lookup(ResolvedRequestPolicies(_AUTH, _USER, _HASH, (plan,))))


@pytest.mark.asyncio
async def test_disabled_policies_add_no_lookup_and_preserve_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", "false")
    source: Final = body()
    admitted: Final = await runtime.authorize_request_policies(request(), _AUTH, source, "/v1/chat/completions",
        lookup=Lookup(RequestPolicyFailure("forbidden")))
    assert admitted is _AUTH
    assert runtime.prepare_request_policy(admitted, source, "acompletion") is source
    assert runtime.final_request_policy_deployment(source, "acompletion") is source


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", (("user_id", "different-owner"), ("user_role", "proxy_admin"),
    ("created_at", datetime(2026, 1, 2, tzinfo=timezone.utc)), ("token", "b" * 64)))
async def test_fresh_native_authority_cannot_be_replaced_by_cached_identity(field: str, value: object) -> None:
    changed: Final = _AUTH.model_copy(update={field: value})
    result: Final = ResolvedRequestPolicies(changed, _USER, "b" * 64 if field == "token" else _HASH, ())
    with pytest.raises(HTTPException) as denied:
        await runtime.authorize_request_policies(request(), _AUTH, body(), "/v1/chat/completions", lookup=Lookup(result))
    assert denied.value.status_code == 403 and _HASH not in str(denied.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", (("extra_body", {"max_tokens": 999}), ("drop_params", True),
    ("api_key", "sk-fixture-override"), ("api_base", "https://override.invalid"), ("callbacks", []), ("background", True)))
async def test_bound_calls_refuse_routing_or_callback_bypass_before_preparation(key: str, value: JsonValue) -> None:
    with pytest.raises(HTTPException) as denied:
        await authorized(data=body({key: value}))
    assert denied.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("data", (
    body({"system": "client system"}), body({"instructions": "client instruction"}),
    body({"messages": [{"role": "developer", "content": "override"}]}),
    body({"messages": [{"role": "system", "content": "override"}]}),
    body({"input": [{"role": "developer", "content": "override"}]}),
    body({"previous_response_id": "opaque-history"}),
))
async def test_private_prompt_refuses_client_instruction_conflicts(data: Mapping[str, object]) -> None:
    with pytest.raises(HTTPException) as denied:
        await authorized(prompt="private server instruction", data=data)
    assert denied.value.status_code == 400 and "private server" not in str(denied.value)


@pytest.mark.asyncio
async def test_policy_is_applied_before_quote_but_private_prompt_only_reaches_final_deployment() -> None:
    auth: Final = await authorized(prompt="private server instruction")
    source: Final = body({"max_completion_tokens": 1000, "temperature": 1.5})
    prepared: Final = runtime.prepare_request_policy(auth, source, "acompletion")
    assert prepared["max_completion_tokens"] == 128 and prepared["temperature"] == 0.5
    assert prepared["messages"] == source["messages"] and "private server" not in str(prepared)
    quoted: Final = runtime.request_policy_quote_data(prepared)
    final: Final = runtime.final_request_policy_deployment({**prepared, "model": "openai/fixture-backend", "model_info": {"id": "fixture-deployment"}}, "acompletion")
    assert quoted["messages"] == final["messages"] == [{"role": "system", "content": "private server instruction"}, {"role": "user", "content": "client input"}]
    assert auth.metadata[runtime.REQUEST_POLICY_PROMPT_MARKER] is True
    assert runtime.request_policy_prompt_active() and runtime.request_policy_user() is _USER
    assert "private server" not in repr(runtime._context.get())


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", (
    {"max_tokens": 129}, {"max_tokens": True}, {"max_tokens": 128, "max_output_tokens": 128},
    {"temperature": 0.1}, {"drop_params": True}, {"extra_body": {"temperature": 0.1}},
    {"model": "openai/other"}, {"model_info": {"id": "other-deployment"}},
))
async def test_final_guard_refuses_callback_or_deployment_changes(changed: Mapping[str, object]) -> None:
    auth: Final = await authorized()
    prepared: Final = runtime.prepare_request_policy(auth, body(), "acompletion")
    with pytest.raises(ManagedCredentialRequestRejected):
        runtime.final_request_policy_deployment({**prepared, "model": "openai/fixture-backend", "model_info": {"id": "fixture-deployment"}, **changed}, "acompletion")


@pytest.mark.asyncio
@pytest.mark.parametrize("path,call_type,output", (("/v1/responses", "aresponses", "max_output_tokens"),
    ("/v1/messages", "aanthropic_messages", "max_tokens")))
async def test_supported_route_aliases_use_their_native_output_field(path: str, call_type: str, output: str) -> None:
    auth: Final = await authorized(route=path)
    prepared: Final = runtime.prepare_request_policy(auth, body(), call_type)
    assert prepared[output] == 128
    assert runtime.final_request_policy_deployment({**prepared, "model": "openai/fixture-backend", "model_info": {"id": "fixture-deployment"}}, call_type)[output] == 128


class MutatingCallback(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs: Mapping[str, object], call_type: object) -> dict[str, object]:  # mutable-ok: the native SDK callback contract requires mutable kwargs
        return {**kwargs, "max_tokens": 999}


@pytest.mark.asyncio
async def test_native_deployment_hook_enforces_after_all_operator_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.utils import async_pre_call_deployment_hook

    auth: Final = await authorized()
    prepared: Final = runtime.prepare_request_policy(auth, body(), "acompletion")
    monkeypatch.setattr(litellm, "callbacks", (MutatingCallback(),))
    with pytest.raises(ManagedCredentialRequestRejected):
        await async_pre_call_deployment_hook({**prepared, "model": "openai/fixture-backend", "model_info": {"id": "fixture-deployment"}}, "acompletion")


@pytest.mark.asyncio
async def test_final_prompt_rejects_callback_instruction_conflicts_and_never_duplicates_own_prompt() -> None:
    auth: Final = await authorized(prompt="private server instruction")
    prepared: Final = runtime.prepare_request_policy(auth, body(), "acompletion")
    target: Final = {**prepared, "model": "openai/fixture-backend", "model_info": {"id": "fixture-deployment"}}
    with pytest.raises(ManagedCredentialRequestRejected):
        runtime.final_request_policy_deployment({**target, "messages": [{"role": "developer", "content": "override"}]}, "acompletion")
    first: Final = runtime.final_request_policy_deployment(target, "acompletion")
    second: Final = runtime.final_request_policy_deployment(first, "acompletion")
    assert second["messages"] == first["messages"]
    assert runtime.REQUEST_POLICY_PROMPT_MARKER not in first["metadata"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route,definition", (
    ("/v1/embeddings", Definition()),
    ("/v1/responses", Definition(allowed_routes=("chat.completions",))),
    ("/v1/responses", Definition(seed=1)), ("/v1/messages", Definition(seed=1)),
))
async def test_bound_unsupported_route_or_parameters_fail_before_wallet(route: str, definition: Definition) -> None:
    with pytest.raises(HTTPException) as denied:
        await authorized(route=route, definition=definition)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_final_and_recovered_identity_cannot_lose_the_authenticated_attestation() -> None:
    auth: Final = await authorized()
    with pytest.raises(HTTPException) as denied:
        runtime.require_request_policy_result(auth.model_copy(update={"user_role": "proxy_admin"}), inference=True)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_policy_rejection_clears_previous_billing_handle_before_any_policy_work() -> None:
    from litellm.proxy.retail import billing

    auth: Final = await authorized()
    billing._active_reservation.set("previous-request-handle")
    with pytest.raises(HTTPException) as denied:
        await billing.RetailBilling()._prepare_request(auth, body({"extra_body": {"max_tokens": 999}}), "acompletion")
    assert denied.value.status_code == 400 and billing._active_reservation.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ("models", "budget"))
async def test_fresh_native_key_revalidation_refuses_cached_model_grants_or_budget(limit: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.proxy.auth.user_api_key_auth import revalidate_request_policy_key

    native_globals(monkeypatch)
    current: Final = _AUTH.model_copy(update={
        "models": ["revoked-model"] if limit == "models" else ["fixture-model"],
        "max_budget": 1 if limit == "budget" else 7, "spend": 2 if limit == "budget" else 0,
    })
    selected: Final = request()
    data: Final = {"model": "fixture-model"}
    admitted: Final = await runtime.authorize_request_policies(selected, _AUTH, data, "/v1/chat/completions",
        lookup=Lookup(ResolvedRequestPolicies(current, _USER, _HASH, ())))
    assert admitted.models == current.models and admitted.max_budget == current.max_budget
    with pytest.raises((ProxyException, litellm.BudgetExceededError)) as denied:
        await revalidate_request_policy_key(selected, admitted, data, "/v1/chat/completions")
    assert "model" in str(denied.value).lower() if limit == "models" else isinstance(denied.value, litellm.BudgetExceededError)


@pytest.mark.asyncio
async def test_revalidation_restores_original_server_proofs_without_restoring_cached_grants() -> None:
    original: Final = _AUTH.model_copy(update={"via_virtual_key": True, "is_session_token": True, "models": ["old"]})
    checked: Final = _AUTH.model_copy(update={"models": ["new"], "max_budget": 1})
    restored: Final = runtime.restore_request_policy_proofs(original, checked)
    assert restored.via_virtual_key and restored.is_session_token
    assert restored.models == ["new"] and restored.max_budget == 1


def test_model_catalog_is_not_treated_as_an_inference_call() -> None:
    assert not runtime.request_policy_inference("/v1/models")
    assert runtime.request_policy_inference("/v1/chat/completions")


@pytest.mark.parametrize("required_auth", (False, True))
def test_passthrough_detection_uses_actual_parameterized_route_before_registry_lookup(
    required_auth: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litellm.proxy.pass_through_endpoints import pass_through_endpoints as passthrough

    monkeypatch.setattr(passthrough, "_registered_pass_through_routes", {})
    app: Final = FastAPI()
    passthrough.InitPassThroughEndpointHelpers.add_exact_path_route(app, "/raw/{item}", "https://fixture.invalid",
        None, False, False, None, None, "fixture-policy-route", auth=required_auth, methods=["POST"])
    scope: Final = {"type": "http", "app": app, "path": "/raw/concrete", "method": "POST", "root_path": ""}
    assert runtime.request_policy_passthrough_denied(scope)
    assert not runtime.request_policy_passthrough_denied({**scope, "method": "GET"})


@pytest.mark.asyncio
async def test_empty_plan_cannot_be_switched_to_a_different_model_or_provider() -> None:
    selected: Final = request()
    source: Final = body()
    auth: Final = await runtime.authorize_request_policies(selected, _AUTH, source, "/v1/chat/completions",
        lookup=Lookup(ResolvedRequestPolicies(_AUTH, _USER, _HASH, ())))
    with pytest.raises(HTTPException):
        runtime.prepare_request_policy(auth, {**source, "model": "other-policy-model"}, "acompletion")
    prepared: Final = runtime.prepare_request_policy(auth, source, "acompletion")
    with pytest.raises(ManagedCredentialRequestRejected):
        runtime.final_request_policy_deployment({**prepared, "model": "openai/other-policy-model", "model_info": {"id": "other-deployment"}}, "acompletion")
    with pytest.raises(HTTPException) as denied:
        await runtime.authorize_request_policies(selected, _AUTH, body({"api_base": "https://override.invalid"}), "/v1/chat/completions",
            lookup=Lookup(ResolvedRequestPolicies(_AUTH, _USER, _HASH, ())))
    assert denied.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("max_tokens", "temperature", "top_p", "seed"))
async def test_callback_booleans_cannot_equal_fixed_numeric_one(field: str) -> None:
    auth: Final = await authorized(definition=Definition(max_output_tokens=1, temperature=1, top_p=1, seed=1))
    prepared: Final = runtime.prepare_request_policy(auth, body(), "acompletion")
    with pytest.raises(ManagedCredentialRequestRejected):
        runtime.final_request_policy_deployment({**prepared, "model": "openai/fixture-backend",
            "model_info": {"id": "fixture-deployment"}, field: True}, "acompletion")


@pytest.mark.asyncio
@pytest.mark.parametrize("cap_low,client_low,status", ((True, False, 403), (False, True, 400), (True, True, 403)))
async def test_responses_minimum_rejects_before_deployment_attestation(cap_low: bool, client_low: bool, status: int) -> None:
    from litellm.llms.openai.responses.transformation import OPENAI_RESPONSES_API_MIN_MAX_OUTPUT_TOKENS

    minimum: Final = OPENAI_RESPONSES_API_MIN_MAX_OUTPUT_TOKENS
    source: Final = {"model": "fixture-model", "input": "client input",
        "max_output_tokens": minimum - 1 if client_low else minimum}
    auth: Final = await authorized(route="/v1/responses", data=source,
        definition=Definition(max_output_tokens=minimum - 1 if cap_low else minimum))
    with pytest.raises(HTTPException) as denied:
        runtime.prepare_request_policy(auth, source, "aresponses")
    assert denied.value.status_code == status
    with pytest.raises(ManagedCredentialRequestRejected):
        runtime.final_request_policy_deployment({**source, "model": "openai/fixture-backend",
            "model_info": {"id": "fixture-deployment"}}, "aresponses")


@pytest.mark.asyncio
async def test_responses_supported_minimum_is_preserved_exactly_at_final_boundary() -> None:
    from litellm.llms.openai.responses.transformation import OPENAI_RESPONSES_API_MIN_MAX_OUTPUT_TOKENS

    minimum: Final = OPENAI_RESPONSES_API_MIN_MAX_OUTPUT_TOKENS
    auth: Final = await authorized(route="/v1/responses", definition=Definition(max_output_tokens=minimum))
    prepared: Final = runtime.prepare_request_policy(auth, body(), "aresponses")
    assert prepared["max_output_tokens"] == minimum
    final: Final = runtime.final_request_policy_deployment({**prepared, "model": "openai/fixture-backend",
        "model_info": {"id": "fixture-deployment"}}, "aresponses")
    assert final["max_output_tokens"] == minimum
