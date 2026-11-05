import os
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final

from fastapi import HTTPException, Request
from starlette.applications import Starlette
from starlette.routing import Match, Route
from starlette.types import Scope

from litellm.proxy._types import (
    UI_TEAM_ID,
    LiteLLM_BudgetTable,
    LiteLLM_TeamMembership,
    LiteLLM_TeamTableCachedObj,
    LiteLLM_UserTable,
    UserAPIKeyAuth,
)
from litellm.proxy.auth.route_checks import RouteChecks
from litellm.proxy.auth.team_grants import team_grants
from litellm.proxy.utils import hash_token_if_needed

from .api_jwt_auth import api_jwt_compatible
from .control_plane_access import native_master_actor
from .coordination_guard import coordination_guard_status
from .pricing import as_mapping
from .request_policy_types import (
    CompiledRequestPolicy,
    RequestPolicyFailure,
    ResolvedRequestPolicies,
    RouteKind,
    compile_policies,
)

REQUEST_POLICY_PROMPT_MARKER: Final = "retail_request_policy_prompt"
_ROUTES: Final[Mapping[str, RouteKind]] = {
    "/chat/completions": "chat.completions", "/v1/chat/completions": "chat.completions",
    "/responses": "responses", "/v1/responses": "responses", "/messages": "messages", "/v1/messages": "messages",
}
_CALLS: Final[Mapping[str, RouteKind]] = {
    "completion": "chat.completions", "acompletion": "chat.completions",
    "responses": "responses", "aresponses": "responses",
    "anthropic_messages": "messages", "aanthropic_messages": "messages",
}
_OVERRIDES: Final = frozenset((
    "api_base", "api_key", "base_url", "custom_llm_provider", "litellm_params", "model_list",
    "aws_access_key_id", "aws_secret_access_key", "aws_session_token", "vertex_credentials", "azure_ad_token",
    "fallbacks", "context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks", "deployment_id",
    "specific_deployment", "extra_body", "extra_headers", "headers", "allowed_openai_params",
    "callbacks", "success_callback", "failure_callback", "input_callback", "disable_callbacks", "mock_response",
    "user_config", "preset_cache_key", "cache_key", "redis_namespace",
))
_OUTPUTS: Final = ("max_tokens", "max_completion_tokens", "max_output_tokens")
_FAILED: Final = "This request does not satisfy the configured request policy"
_UNAVAILABLE: Final = "Request policy authority is temporarily unavailable"


class _Prepared:
    __slots__ = ("deployments", "output_key", "output_limit", "ready")

    def __init__(self) -> None:
        self.output_key: str | None = None
        self.output_limit: int | None = None
        self.deployments: tuple[tuple[str, str], ...] = ()
        self.ready = False

    def bind(self, key: str | None, limit: int | None, deployments: tuple[tuple[str, str], ...]) -> None:
        if self.ready and (self.output_key, self.output_limit, self.deployments) != (key, limit, deployments):
            raise HTTPException(403, _FAILED)
        self.output_key = key
        self.output_limit = limit
        self.deployments = deployments
        self.ready = True


@dataclass(frozen=True, slots=True)
class _Context:
    auth: UserAPIKeyAuth = field(repr=False)
    user: LiteLLM_UserTable | None = field(repr=False)
    model: str = field(repr=False)
    route: RouteKind | None
    compiled: CompiledRequestPolicy = field(repr=False)
    bound: bool
    team: LiteLLM_TeamTableCachedObj | None = field(default=None, repr=False)
    team_membership: LiteLLM_TeamMembership | None = field(default=None, repr=False)
    team_member_default_budget: LiteLLM_BudgetTable | None = field(default=None, repr=False)
    prepared: _Prepared = field(default_factory=_Prepared, repr=False)


_context: Final[ContextVar[_Context | None]] = ContextVar("retail_request_policy", default=None)


def _enabled() -> bool:
    return os.environ.get("RETAIL_REQUEST_POLICIES_ENABLED", "false").lower() == "true"


def _require_environment() -> None:
    from .control_plane_flags import request_policies_enabled

    health: Final = coordination_guard_status()
    if not request_policies_enabled() or not api_jwt_compatible() or not health.required or not health.accepting_credentials:
        raise HTTPException(503, _UNAVAILABLE)


def request_policy_prompt_active() -> bool:
    context: Final = _context.get()
    return context is not None and context.compiled.prompt is not None


def request_policy_bound() -> bool:
    context: Final = _context.get()
    return _enabled() and context is not None and context.bound


def request_policy_attested() -> bool:
    return _enabled() and _context.get() is not None


def reset_request_policy_context() -> None:
    from .request_policy_privacy import mark_private_policy_prompt

    _context.set(None)
    mark_private_policy_prompt(False)


def request_policy_prompt_texts() -> tuple[str, ...]:
    context: Final = _context.get()
    return (context.compiled.prompt.get_secret_value(),) if context is not None and context.compiled.prompt else ()


def request_policy_user() -> LiteLLM_UserTable | None:
    context: Final = _context.get()
    return context.user if _enabled() and context is not None else None


def request_policy_team() -> LiteLLM_TeamTableCachedObj | None:
    context: Final = _context.get()
    return context.team if _enabled() and context is not None else None


def request_policy_team_membership() -> LiteLLM_TeamMembership | None:
    context: Final = _context.get()
    return context.team_membership if _enabled() and context is not None else None


def request_policy_team_member_default_budget() -> LiteLLM_BudgetTable | None:
    context: Final = _context.get()
    return context.team_member_default_budget if _enabled() and context is not None else None


def _fresh_team_grants(resolved: ResolvedRequestPolicies) -> UserAPIKeyAuth:
    team: Final = resolved.team
    membership: Final = resolved.team_membership
    if (resolved.auth.team_id in (None, UI_TEAM_ID)) != (team is None) or team is not None and (
        team.team_id != resolved.auth.team_id or team.blocked
    ) or membership is not None and (
        membership.team_id != resolved.auth.team_id or membership.user_id != resolved.auth.user_id
    ):
        raise HTTPException(403, _FAILED)
    defaults: Final = UserAPIKeyAuth()
    cleared: Final = {name: getattr(defaults, name) for name in UserAPIKeyAuth.model_fields if name.startswith("team_")}
    return resolved.auth.model_copy(update={**cleared, "team_id": resolved.auth.team_id,
        **team_grants(team, membership, resolved.auth.user_id)})


def request_policy_inference(route: str) -> bool:
    return RouteChecks.is_llm_api_route(route) and not RouteChecks.is_info_route(route)


def request_policy_passthrough_denied(scope: Scope) -> bool:
    if not _enabled() or scope.get("type") not in {"http", "websocket"}:
        return False
    app: Final = scope.get("app")
    if not isinstance(app, Starlette):
        return False
    for route in app.routes:
        matched, _child = route.matches(scope)
        if matched is not Match.FULL:
            continue
        if not isinstance(route, Route):
            return False
        endpoint: Final = route.endpoint
        return "create_pass_through_route." in getattr(endpoint, "__qualname__", "") or getattr(endpoint, "__module__", "") in {
            "litellm.proxy.pass_through_endpoints.llm_passthrough_endpoints",
            "litellm.proxy.pass_through_endpoints.openai_passthrough_endpoints",
        }
    return False


def _creation(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


async def _lookup(auth: UserAPIKeyAuth, model: str) -> ResolvedRequestPolicies | RequestPolicyFailure:
    from litellm.proxy import proxy_server

    from .request_policy_store import NativeRequestPolicyStore

    if proxy_server.prisma_client is None:
        return RequestPolicyFailure("unavailable")
    return await NativeRequestPolicyStore(proxy_server.prisma_client).lookup(
        auth.user_id, hash_token_if_needed(auth.token or auth.api_key or ""), model,
        trusted_master=native_master_actor(auth, bool(proxy_server.master_key)),
    )


def _client_body(context: _Context, data: Mapping[str, object]) -> None:
    if any(data.get(key) is not None for key in _OVERRIDES) or any(data.get(key) for key in ("drop_params", "background", "batch")):
        raise HTTPException(400, _FAILED)
    if not context.bound:
        return
    import litellm

    if litellm.drop_params:
        raise HTTPException(503, _UNAVAILABLE)
    if context.route is None or context.compiled.allowed_routes is not None and context.route not in context.compiled.allowed_routes:
        raise HTTPException(403, _FAILED)
    if context.compiled.seed is not None and context.route != "chat.completions":
        raise HTTPException(403, _FAILED)
    if context.route == "messages" and context.compiled.temperature is not None and context.compiled.temperature > 1:
        raise HTTPException(403, _FAILED)
    if context.compiled.prompt is None:
        return
    messages: Final = data.get("messages")
    inputs: Final = data.get("input")
    if any(data.get(key) is not None for key in (
        "system", "instructions", "system_instruction", "systemInstruction", "previous_response_id", "conversation",
    )) or (
        isinstance(messages, list) and any(as_mapping(message).get("role") in {"system", "developer"} for message in messages)
    ) or (
        isinstance(inputs, list) and any(as_mapping(item).get("role") in {"system", "developer"} for item in inputs)
    ):
        raise HTTPException(400, _FAILED)


async def authorize_request_policies(
    request: Request, auth: UserAPIKeyAuth, data: Mapping[str, object], route: str,
    *, lookup: Callable[[UserAPIKeyAuth, str], Awaitable[ResolvedRequestPolicies | RequestPolicyFailure]] = _lookup,
) -> UserAPIKeyAuth:
    from .request_policy_privacy import mark_private_policy_prompt, private_policy_authority_io

    reset_request_policy_context()
    if not _enabled() or not request_policy_inference(route):
        return auth
    _require_environment()
    model: Final = data.get("model")
    selected_model: Final = model if isinstance(model, str) else ""
    try:
        with private_policy_authority_io():
            resolved: Final = await lookup(auth, selected_model)
    except Exception:  # noqa: BLE001  # authority failures must not disclose native identity or policy details
        raise HTTPException(503, _UNAVAILABLE) from None
    if isinstance(resolved, RequestPolicyFailure):
        raise HTTPException(503 if resolved.code == "unavailable" else 403, _UNAVAILABLE if resolved.code == "unavailable" else _FAILED)
    if (
        auth.user_id != resolved.auth.user_id or auth.user_role != resolved.auth.user_role
        or hash_token_if_needed(auth.token or auth.api_key or "") != resolved.target_hash
        or auth.created_at is not None and _creation(auth.created_at) != _creation(resolved.auth.created_at)
    ):
        raise HTTPException(403, _FAILED)
    compiled: Final = compile_policies(resolved.policies)
    if isinstance(compiled, RequestPolicyFailure):
        raise HTTPException(403, _FAILED)
    route_kind: Final = _ROUTES.get(route) if request.method == "POST" else None
    fresh_auth: Final = _fresh_team_grants(resolved)
    team: Final = LiteLLM_TeamTableCachedObj.model_validate(resolved.team.model_dump()) if resolved.team is not None else None
    context: Final = _Context(fresh_auth, resolved.user, selected_model, route_kind, compiled, bool(resolved.policies),
        team, resolved.team_membership, resolved.team_member_default_budget)
    _client_body(context, data)
    if context.bound and request.headers.get("x-litellm-disable-callbacks") is not None:
        raise HTTPException(400, _FAILED)
    _context.set(context)
    if compiled.prompt is not None:
        mark_private_policy_prompt(True)
        request.scope[REQUEST_POLICY_PROMPT_MARKER] = True  # rebind-ok: trusted private marker must cross ASGI task boundaries
    authority_fields: Final = {name: getattr(fresh_auth, name) for name in fresh_auth.model_fields_set
        if UserAPIKeyAuth.model_fields[name].exclude is not True}
    refreshed: Final = auth.model_copy(update={**authority_fields,
        "org_id": fresh_auth.org_id, "organization_alias": fresh_auth.organization_alias,
        "organization_max_budget": fresh_auth.organization_max_budget, "organization_tpm_limit": fresh_auth.organization_tpm_limit,
        "organization_rpm_limit": fresh_auth.organization_rpm_limit, "organization_metadata": fresh_auth.organization_metadata,
        "team_budget_snapshot": None, "org_budget_snapshot": None})
    return refreshed.model_copy(update={"metadata": {**(refreshed.metadata or {}), REQUEST_POLICY_PROMPT_MARKER: True}}) if compiled.prompt else refreshed


def request_policy_revalidate_key(auth: UserAPIKeyAuth) -> bool:
    from litellm.proxy import proxy_server

    return _enabled() and _context.get() is not None and not native_master_actor(auth, bool(proxy_server.master_key))


def restore_request_policy_proofs(auth: UserAPIKeyAuth, checked: UserAPIKeyAuth) -> UserAPIKeyAuth:
    return checked.model_copy(update={
        "via_virtual_key": auth.via_virtual_key, "authenticated_by_custom_auth": auth.authenticated_by_custom_auth,
        "is_session_token": auth.is_session_token, "jwt_claims": auth.jwt_claims, "agent_caller": auth.agent_caller,
        "mcp_admitted_user_subject": auth.mcp_admitted_user_subject, "requires_fresh_policy": auth.requires_fresh_policy,
        "mcp_explicit_grants_only": auth.mcp_explicit_grants_only, "mcp_source_team_rpm_limits": auth.mcp_source_team_rpm_limits,
        "mcp_session_resource_server_id": auth.mcp_session_resource_server_id, "mcp_toolset_id": auth.mcp_toolset_id,
        "managed_agent_context": auth.managed_agent_context, "managed_agent_policy": auth.managed_agent_policy,
        "invoked_agent_id": auth.invoked_agent_id, "invoked_agent_policy": auth.invoked_agent_policy,
        "agent_invocation_cost": auth.agent_invocation_cost, "billing_agent_policy": auth.billing_agent_policy,
    })


def require_request_policy_result(auth: UserAPIKeyAuth, *, inference: bool = False) -> None:
    if not _enabled():
        return
    context: Final = _context.get()
    if context is None:
        if inference:
            raise HTTPException(503, _UNAVAILABLE)
        return
    _require_environment()
    if auth.user_id != context.auth.user_id or auth.user_role != context.auth.user_role or auth.team_id != context.auth.team_id or (
        hash_token_if_needed(auth.token or auth.api_key or "") != hash_token_if_needed(context.auth.token or context.auth.api_key or "")
    ):
        raise HTTPException(403, _FAILED)


def _output(context: _Context, data: Mapping[str, object]) -> tuple[str | None, int | None]:
    provided: Final = tuple(key for key in _OUTPUTS if data.get(key) is not None)
    if len(provided) > 1 or any(
        key != "max_output_tokens" if context.route == "responses" else
        key != "max_tokens" if context.route == "messages" else key == "max_output_tokens" for key in provided
    ):
        raise HTTPException(400, _FAILED)
    key: Final = provided[0] if provided else "max_output_tokens" if context.route == "responses" else "max_tokens"
    requested: Final = data.get(key)
    if requested is not None and (not isinstance(requested, int) or isinstance(requested, bool) or not 1 <= requested <= 1_000_000):
        raise HTTPException(400, _FAILED)
    cap: Final = context.compiled.max_output_tokens
    if context.route == "responses":
        from litellm.llms.openai.responses.transformation import OPENAI_RESPONSES_API_MIN_MAX_OUTPUT_TOKENS

        if cap is not None and cap < OPENAI_RESPONSES_API_MIN_MAX_OUTPUT_TOKENS:
            raise HTTPException(403, _FAILED)
        if isinstance(requested, int) and requested < OPENAI_RESPONSES_API_MIN_MAX_OUTPUT_TOKENS:
            raise HTTPException(400, _FAILED)
    if isinstance(requested, int):
        return key, min(requested, cap) if cap is not None else requested
    return (key, cap) if cap is not None else (None, None)


def prepare_request_policy(auth: UserAPIKeyAuth, data: Mapping[str, object], call_type: str) -> Mapping[str, object]:
    if not _enabled():
        return data
    context: Final = _context.get()
    if context is None:
        raise HTTPException(503, _UNAVAILABLE)
    require_request_policy_result(auth)
    if context.route != _CALLS.get(call_type) or data.get("model") != context.model:
        raise HTTPException(403, _FAILED)
    _client_body(context, data)
    from litellm.proxy import proxy_server

    deployments: Final = proxy_server.llm_router.get_model_list(context.model, team_id=auth.team_id) if proxy_server.llm_router else ()
    approved: Final = tuple((str(as_mapping(item.get("model_info")).get("id", "")),
        str(as_mapping(item.get("litellm_params")).get("model", ""))) for item in deployments)
    if not approved:
        raise HTTPException(503, _UNAVAILABLE)
    if not context.bound:
        context.prepared.bind(None, None, approved)
        return data
    key, limit = _output(context, data)
    context.prepared.bind(key, limit, approved)
    fixed: Final = {name: value for name, value in (("temperature", context.compiled.temperature),
        ("top_p", context.compiled.top_p), ("seed", context.compiled.seed)) if value is not None}
    marker: Final = {REQUEST_POLICY_PROMPT_MARKER: True} if context.compiled.prompt else {}
    return {**data, **fixed, **({key: limit} if key is not None else {}),
        "metadata": {**as_mapping(data.get("metadata")), **marker},
        "litellm_metadata": {**as_mapping(data.get("litellm_metadata")), **marker}}


def request_policy_quote_data(data: Mapping[str, object]) -> Mapping[str, object]:
    if not _enabled():
        return data
    context: Final = _context.get()
    return _with_prompt(context, data) if context is not None and context.compiled.prompt is not None else data


def _with_prompt(context: _Context, data: Mapping[str, object]) -> Mapping[str, object]:
    prompt: Final = context.compiled.prompt.get_secret_value() if context.compiled.prompt else ""
    if context.route == "responses":
        return {**data, "instructions": prompt}
    if context.route == "messages":
        return {**data, "system": prompt}
    messages: Final = data.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(400, _FAILED)
    return {**data, "messages": [{"role": "system", "content": prompt}, *messages]}


def _prompt_source(context: _Context, data: Mapping[str, object]) -> Mapping[str, object]:
    if context.compiled.prompt is None:
        return data
    prompt: Final = context.compiled.prompt.get_secret_value()
    system: Final = data.get("system")
    instructions: Final = data.get("instructions")
    if system not in (None, prompt) or instructions not in (None, prompt):
        raise HTTPException(403, _FAILED)
    messages: Final = data.get("messages")
    inputs: Final = data.get("input")
    own_first: Final = isinstance(messages, list) and bool(messages) and as_mapping(messages[0]) == {"role": "system", "content": prompt}
    original: Final = messages[1:] if own_first and isinstance(messages, list) else messages
    if isinstance(original, list) and any(as_mapping(item).get("role") in {"system", "developer"} for item in original):
        raise HTTPException(403, _FAILED)
    if isinstance(inputs, list) and any(as_mapping(item).get("role") in {"system", "developer"} for item in inputs):
        raise HTTPException(403, _FAILED)
    return {**data, **({"messages": original} if own_first else {})}


def _final_deployment(context: _Context, data: Mapping[str, object], call_type: str) -> Mapping[str, object]:
    _require_environment()
    prepared: Final = context.prepared
    if not prepared.ready or context.route != _CALLS.get(call_type):
        raise HTTPException(403, _FAILED)
    selected: Final = (str(as_mapping(data.get("model_info")).get("id", "")), str(data.get("model", "")))
    if selected not in prepared.deployments or data.get("drop_params") or data.get("extra_body"):
        raise HTTPException(403, _FAILED)
    provided: Final = tuple(key for key in _OUTPUTS if data.get(key) is not None)
    actual_output: Final = data.get(prepared.output_key) if prepared.output_key is not None else None
    if prepared.output_key is not None and (
        provided != (prepared.output_key,) or not isinstance(actual_output, int) or isinstance(actual_output, bool)
        or actual_output != prepared.output_limit
    ):
        raise HTTPException(403, _FAILED)
    if any(not _same_fixed(name, data.get(name), value) for name, value in (("temperature", context.compiled.temperature),
        ("top_p", context.compiled.top_p), ("seed", context.compiled.seed)) if value is not None):
        raise HTTPException(403, _FAILED)
    if context.compiled.prompt is None:
        return data
    outbound: Final = {**data, "metadata": {
        key: value for key, value in as_mapping(data.get("metadata")).items() if key != REQUEST_POLICY_PROMPT_MARKER
    }}
    return _with_prompt(context, _prompt_source(context, outbound))


def _same_fixed(name: str, actual: object, expected: float) -> bool:
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    return actual == expected and (name != "seed" or isinstance(actual, int))


def final_request_policy_deployment(data: Mapping[str, object], call_type: str) -> Mapping[str, object]:
    if not _enabled():
        return data
    from litellm.router_utils.managed_credential_errors import (
        ManagedCredentialRequestRejected,
        ManagedCredentialUnavailable,
    )

    context: Final = _context.get()
    if context is None:
        raise ManagedCredentialUnavailable(str(data.get("model", "")))
    try:
        return _final_deployment(context, data, call_type)
    except HTTPException as exc:
        if exc.status_code == 503:
            raise ManagedCredentialUnavailable(context.model) from None
        raise ManagedCredentialRequestRejected(context.model) from None
