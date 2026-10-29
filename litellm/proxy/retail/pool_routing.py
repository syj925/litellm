from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final

from fastapi import HTTPException

from .pool_store import PoolRouteDecision
from .pricing import as_mapping


class _AttemptEvidence:
    __slots__ = ("attempts", "denied", "uncertain")

    def __init__(self) -> None:
        self.attempts: int = 0
        self.denied: bool = False
        self.uncertain: bool = False


@dataclass(frozen=True, slots=True)
class _RouteContext:
    model: str
    decision: PoolRouteDecision
    evidence: _AttemptEvidence


_route: Final[ContextVar[_RouteContext | None]] = ContextVar("retail_resource_route", default=None)
_NON_BILLABLE_REJECTIONS: Final = frozenset({400, 401, 403, 404, 413, 422, 429})
_CLIENT_ROUTING_OVERRIDES: Final = frozenset(
    {
        "api_base",
        "api_key",
        "base_url",
        "custom_llm_provider",
        "litellm_params",
        "model_list",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "vertex_credentials",
        "azure_ad_token",
        "fallbacks",
        "context_window_fallbacks",
        "content_policy_fallbacks",
        "default_fallbacks",
        "deployment_id",
        "specific_deployment",
        "preset_cache_key",
        "cache_key",
        "redis_namespace",
    }
)


def reset_pool_routing() -> None:
    _route.set(None)


def bind_pool_routing(model: str, decision: PoolRouteDecision | None, data: Mapping[str, object]) -> None:
    if decision is None:
        return
    if any(data.get(key) is not None for key in _CLIENT_ROUTING_OVERRIDES):
        raise HTTPException(
            400, "Resource routing is managed by your API key. Request-level routing overrides are disabled"
        )
    _route.set(_RouteContext(model=model, decision=decision, evidence=_AttemptEvidence()))


def _deployment_id(deployment: Mapping[str, object]) -> str | None:
    value: Final = as_mapping(deployment.get("model_info")).get("id")
    return value if isinstance(value, str) and value else None


def filter_pool_deployments(deployments: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    context: Final = _route.get()
    if context is None:
        return [dict(deployment) for deployment in deployments]
    bindings: Final = {binding.deployment_id: binding for binding in context.decision.bindings}
    eligible: Final = tuple(
        deployment
        for deployment in deployments
        if _deployment_id(deployment) in context.decision.allowed_deployment_ids
    )
    if not eligible:
        context.evidence.denied = True
        raise HTTPException(
            503, "No eligible deployment remains in the selected resource pool. Cross-pool fallback is disabled"
        )
    return [
        {
            **deployment,
            "litellm_params": {
                **as_mapping(deployment.get("litellm_params")),
                "weight": bindings[str(_deployment_id(deployment))].weight,
                "order": bindings[str(_deployment_id(deployment))].priority,
            },
        }
        for deployment in eligible
    ]


def guard_pool_deployment(kwargs: Mapping[str, object]) -> None:
    context: Final = _route.get()
    if context is None:
        return
    selected: Final = _deployment_id(kwargs)
    if selected not in context.decision.allowed_deployment_ids:
        context.evidence.denied = True
        raise HTTPException(503, "The selected deployment is outside the authorized resource pool")
    context.evidence.attempts += 1


def mark_pool_failure(exception: Exception) -> None:
    context: Final = _route.get()
    if context is not None and getattr(exception, "status_code", None) not in _NON_BILLABLE_REJECTIONS:
        context.evidence.uncertain = True


def pool_rejected_before_upstream() -> bool:
    context: Final = _route.get()
    return context is not None and context.evidence.denied and context.evidence.attempts == 0


def pool_has_uncertain_attempt() -> bool:
    context: Final = _route.get()
    return context is not None and context.evidence.uncertain


def pool_metadata(decision: PoolRouteDecision | None) -> dict[str, str]:
    if decision is None:
        return {}
    return {"retail_resource_pool_id": decision.pool_id, "retail_resource_pool_name": decision.pool_name}


def clean_pool_metadata(value: object, decision: PoolRouteDecision | None = None) -> dict[str, object]:
    return {
        key: item
        for key, item in as_mapping(value).items()
        if not key.startswith("retail_resource_")
        and key != "retail_request_id"
        and (decision is None or key not in {"redis_namespace", "preset_cache_key", "cache_key"})
    }


def pool_cache_scope(user_id: str, decision: PoolRouteDecision | None, data: Mapping[str, object]) -> dict[str, object]:
    if decision is None:
        return {}
    return {"cache": {**as_mapping(data.get("cache")), "namespace": f"retail:{user_id}:{decision.pool_id}"}}
