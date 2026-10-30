import json
import os
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from datetime import datetime
from typing import Final
from uuid import uuid4

from fastapi import HTTPException
from pydantic import JsonValue

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

from .customer_access import customer_resource_policy
from .models import InsufficientFunds, ParallelLimitExceeded
from .pool_routing import (
    bind_pool_routing,
    clean_pool_metadata,
    filter_pool_deployments,
    guard_pool_deployment,
    mark_pool_failure,
    pool_cache_scope,
    pool_has_uncertain_attempt,
    pool_metadata,
    pool_rejected_before_upstream,
    reset_pool_routing,
)
from .pool_store import PoolRouteDecision, PoolStore, router_deployments
from .pricing import (
    as_mapping,
    catalog_snapshot,
    charge_for_usage,
    cost_breakdown,
    reserve_quote,
    usage_facts,
    validate_requested_cache_ttls,
)
from .store import WalletStore, get_store

_SUPPORTED: Final = frozenset(
    {
        "completion",
        "acompletion",
        "text_completion",
        "atext_completion",
        "responses",
        "aresponses",
        "anthropic_messages",
        "aanthropic_messages",
        "embedding",
        "aembedding",
    }
)
_active_reservation: Final[ContextVar[str | None]] = ContextVar("retail_active_reservation", default=None)


def reservation_id(data: Mapping[str, object]) -> str | None:
    containers: Final = (data, as_mapping(data.get("litellm_params")), as_mapping(data.get("standard_logging_object")))
    for container in containers:
        for key in ("metadata", "litellm_metadata"):
            value: Final = as_mapping(container.get(key)).get("retail_request_id")
            if isinstance(value, str) and value:
                return value
    return None


def price_snapshot(
    card: Mapping[str, JsonValue],
    discount: Mapping[str, JsonValue],
    call_type: str,
    pool: PoolRouteDecision | None = None,
) -> dict[str, JsonValue]:
    return {
        **card,
        "version": "token-v2",
        "price_version": card.get("version"),
        "multiplier": str(discount["discount"]),
        "discount": str(discount["discount"]),
        "discount_source": discount["discount_source"],
        "discount_model": discount["model"],
        "plan_id": discount["plan_id"],
        "billing_mode": "embedding" if call_type in {"embedding", "aembedding"} else "text",
        **(
            {
                "resource_pool_id": pool.pool_id,
                "resource_pool_name": pool.pool_name,
                "routing_settings_version": pool.settings_version,
            }
            if pool is not None
            else {}
        ),
    }


def _pool_request_data(
    data: Mapping[str, object], user_id: str, pool: PoolRouteDecision | None, request_id: str | None = None,
) -> dict[str, object]:
    reservation: Final = {"retail_request_id": request_id} if request_id is not None else {}
    return {
        **data,
        **pool_cache_scope(user_id, pool, data),
        "metadata": {**clean_pool_metadata(data.get("metadata"), pool), **pool_metadata(pool), **reservation},
        "litellm_metadata": {
            **clean_pool_metadata(data.get("litellm_metadata"), pool), **pool_metadata(pool), **reservation,
        },
    }


def _resource_policy(auth: UserAPIKeyAuth) -> object | None:
    return customer_resource_policy(auth)


class RetailBilling(CustomLogger):
    is_accounting_callback = True

    def __init__(
        self,
        store: WalletStore | None = None,
        pool_store: PoolStore | None = None,
        deployments: Callable[[], Sequence[Mapping[str, object]]] = router_deployments,
    ) -> None:
        super().__init__()
        self.store = store
        self.pool_store = pool_store
        self.deployments = deployments

    def _store(self) -> WalletStore:
        return self.store if self.store is not None else get_store()

    def _pools(self) -> PoolStore:
        return self.pool_store if self.pool_store is not None else PoolStore(self._store())

    async def _route_exempt_request(
        self, user_api_key_dict: UserAPIKeyAuth, data: Mapping[str, object], policy: object,
    ) -> dict[str, object]:
        user_id: Final = user_api_key_dict.user_id
        if not user_id:
            raise HTTPException(403, "Resource pool routing requires a key owned by a user account")
        model: Final = str(data.get("model", ""))
        pool: Final = await self._pools().resolve_route(user_id, model, policy, self.deployments())
        bind_pool_routing(model, pool, data)
        return _pool_request_data(data, user_id, pool)

    async def async_filter_listed_models(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        model_names: Sequence[str],
    ) -> Sequence[str]:
        policy: Final = _resource_policy(user_api_key_dict)
        exempt: Final = (
            str(user_api_key_dict.user_role) in {"proxy_admin", "LitellmUserRoles.PROXY_ADMIN"}
            or os.getenv("RETAIL_BILLING_ENABLED", "false").lower() != "true"
        )
        if not model_names or (exempt and policy is None):
            return model_names
        allowed: Final = await self._pools().key_model_names(
            user_api_key_dict.user_id or "",
            policy,
            self.deployments(),
        )
        return model_names if allowed is None else tuple(model for model in model_names if model in allowed)

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: object,
        data: dict[str, object],
        call_type: str,
    ) -> dict[str, object]:
        _active_reservation.set(None)
        reset_pool_routing()
        policy: Final = _resource_policy(user_api_key_dict)
        exempt: Final = (
            str(user_api_key_dict.user_role) in {"proxy_admin", "LitellmUserRoles.PROXY_ADMIN"}
            or os.getenv("RETAIL_BILLING_ENABLED", "false").lower() != "true"
        )
        if exempt:
            return data if policy is None else await self._route_exempt_request(user_api_key_dict, data, policy)
        user_id: Final = user_api_key_dict.user_id
        if not user_id:
            raise HTTPException(403, "Wallet billing requires an API key owned by a user")
        if call_type not in _SUPPORTED:
            raise HTTPException(403, "This API endpoint is not enabled for prepaid wallets yet")
        if data.get("background") or data.get("batch"):
            raise HTTPException(400, "Background and batch requests are not enabled for prepaid wallets yet")
        if any(
            data.get(key) is not None
            and (not isinstance(data[key], int) or isinstance(data[key], bool) or data[key] != 1)
            for key in ("n", "best_of")
        ):
            raise HTTPException(400, "Prepaid requests currently support one generated candidate only")
        prompt: Final = data.get("prompt")
        if isinstance(prompt, list) and len(prompt) > 1 and any(isinstance(item, (str, list)) for item in prompt):
            raise HTTPException(400, "Send one text completion prompt per prepaid request")
        tools: Final = data.get("tools")
        if isinstance(tools, list) and any(as_mapping(tool).get("type") not in {"function", "mcp"} for tool in tools):
            raise HTTPException(
                400, "Server-priced tools need a separate price card before they can use a prepaid wallet"
            )
        store: Final = self._store()
        await store.initialize()
        await store.get_wallet(user_id, create=True)
        from litellm.proxy.proxy_server import llm_router

        model: Final = str(data.get("model", ""))
        deployments: Final = llm_router.get_model_list(model, team_id=user_api_key_dict.team_id) if llm_router else ()
        if not deployments:
            raise HTTPException(400, "No configured deployment is available for this model")
        pool: Final = await self._pools().resolve_route(user_id, model, policy, deployments)
        bind_pool_routing(model, pool, data)
        priced_deployments: Final = (
            tuple(
                deployment
                for deployment in deployments
                if str(as_mapping(deployment.get("model_info")).get("id")) in pool.allowed_deployment_ids
            )
            if pool is not None
            else deployments
        )
        price: Final = await store.get_model_price(model)
        try:
            initial: Final = None if price else catalog_snapshot(model, priced_deployments, "1")
            card: Final = price or await store.get_model_price(model, initial)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if card is None:
            raise HTTPException(422, "A model sales price is required")
        discount: Final = (
            await store.get_model_discount(user_id, model, pool_id=pool.pool_id)
            if pool is not None
            else await store.get_model_discount(user_id, model)
        )
        snapshot: Final = price_snapshot(card, discount, call_type, pool)
        try:
            validate_requested_cache_ttls(snapshot, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        embedding: Final = call_type in {"embedding", "aembedding"}
        limit_key: Final = "max_output_tokens" if call_type in {"responses", "aresponses"} else "max_tokens"
        if (
            sum(
                key in data and data[key] is not None
                for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
            )
            > 1
        ):
            raise HTTPException(400, "Send only one output token limit")
        if (
            limit_key == "max_output_tokens"
            and any(data.get(key) is not None for key in ("max_tokens", "max_completion_tokens"))
        ) or (limit_key == "max_tokens" and data.get("max_output_tokens") is not None):
            raise HTTPException(400, "This output token limit is not valid for the requested endpoint")
        requested_limit: Final = data.get("max_completion_tokens", data.get(limit_key, 1024))
        if (
            not isinstance(requested_limit, int)
            or isinstance(requested_limit, bool)
            or not 1 <= requested_limit <= 1_000_000
        ):
            raise HTTPException(400, "Output token limit must be an integer between 1 and 1000000")
        output_limit: Final = 0 if embedding else requested_limit
        request_id: Final = str(uuid4())
        opaque_input: Final = any(
            data.get(key) for key in ("previous_response_id", "conversation", "file", "attachments")
        ) or any(
            marker in json.dumps({key: data.get(key) for key in ("messages", "input", "tools")}, default=str)
            for marker in (
                "image_url",
                "input_image",
                "input_audio",
                "file_id",
                '"mcp"',
                '"image"',
                '"document"',
                '"audio"',
            )
        )
        quote: Final = reserve_quote(
            snapshot, data, output_limit, minimum_input_tokens=1_000_000 if opaque_input else 0
        )
        try:
            await store.reserve(
                user_id, request_id, model, quote, snapshot, max_parallel=int(os.getenv("RETAIL_MAX_PARALLEL", "10"))
            )
        except InsufficientFunds as exc:
            raise HTTPException(
                402, "Insufficient wallet balance for this request. Add funds or lower the output token limit"
            ) from exc
        except ParallelLimitExceeded as exc:
            raise HTTPException(429, "Your wallet has reached its concurrent request limit") from exc
        _active_reservation.set(request_id)
        limit: Final = {} if embedding or "max_completion_tokens" in data else {limit_key: output_limit}
        return {**_pool_request_data(data, user_id, pool, request_id), **limit}

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: list[dict[str, object]],
        messages: list[object] | None,
        request_kwargs: dict[str, object] | None = None,
        parent_otel_span: object = None,
    ) -> list[dict[str, object]]:
        return filter_pool_deployments(healthy_deployments)

    async def async_pre_call_deployment_hook(self, kwargs: Mapping[str, object], call_type: object) -> None:
        guard_pool_deployment(kwargs)

    async def async_post_call_failure_deployment_hook(
        self,
        request_data: Mapping[str, object],
        exception: Exception,
        call_type: object,
        fallback_depth: int | None = None,
    ) -> None:
        mark_pool_failure(exception)

    async def _settle(self, data: Mapping[str, object], response: object, provider_cost: object = None) -> None:
        request_id: Final = reservation_id(data)
        if request_id is None:
            return
        store: Final = self._store()
        reservation: Final = await store.get_reservation(request_id)
        if reservation is None:
            return
        snapshot: Final = as_mapping(reservation["price_snapshot"])
        facts: Final = usage_facts(response, allow_input_only=snapshot.get("billing_mode") == "embedding")
        if facts is None:
            await store.record_pending_usage(
                request_id, {"billing_error": "Reported token usage is missing, incomplete or inconsistent"}
            )
            return
        try:
            amount: Final = charge_for_usage(snapshot, facts)
            breakdown: Final = cost_breakdown(snapshot, facts)
        except ValueError as exc:
            await store.record_pending_usage(request_id, {"usage": facts.model_dump(), "billing_error": str(exc)})
            return
        details: Final[dict[str, JsonValue]] = {
            "usage": facts.model_dump(),
            "cost_breakdown": breakdown,
            "provider_cost": str(provider_cost) if provider_cost is not None else None,
            "gateway_call_id": str(data.get("litellm_call_id", "")),
            "billing_basis": "reported_usage",
        }
        await store.settle(request_id, amount, details)

    async def async_post_call_success_hook(
        self,
        data: dict[str, object],
        user_api_key_dict: UserAPIKeyAuth,
        response: object,
    ) -> None:
        await self._settle(data, response, as_mapping(getattr(response, "_hidden_params", {})).get("response_cost"))

    async def async_log_success_event(
        self,
        kwargs: dict[str, object],
        response_obj: object,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        response: Final = kwargs.get("complete_streaming_response") or response_obj
        try:
            await self._settle(kwargs, response, kwargs.get("response_cost"))
        except Exception:
            verbose_proxy_logger.exception("Wallet settlement remains pending and requires reconciliation")

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, object],
        original_exception: Exception,
        user_api_key_dict: UserAPIKeyAuth,
        traceback_str: str | None = None,
    ) -> None:
        request_id: Final = _active_reservation.get()
        if request_id is None:
            return
        store: Final = self._store()
        reservation: Final = await store.get_reservation(request_id)
        if reservation is None or reservation["status"] != "reserved":
            return
        if pool_rejected_before_upstream():
            await store.release(request_id, "No eligible resource-pool deployment was contacted")
            return
        if pool_has_uncertain_attempt():
            return
        # An interrupted stream or an accounting error is not proof that the provider used nothing.
        if request_data.get("stream") or not isinstance(original_exception, Exception):
            return
        status_code: Final = getattr(original_exception, "status_code", None)
        if status_code in {400, 401, 403, 404, 413, 422, 429}:
            await store.release(request_id, "Request rejected without billable usage")


wallet_callback: Final = RetailBilling()
