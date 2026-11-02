import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import uuid4

from anyio import fail_after
from fastapi import HTTPException
from psycopg import OperationalError
from psycopg.errors import LockNotAvailable, QueryCanceled
from psycopg_pool import PoolTimeout, TooManyRequests
from pydantic import JsonValue

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

from .customer_access import customer_resource_policy
from .database import database_setting
from .expression_billing import catalog_routing_attributes, expression_settlement, expression_token_limits
from .expression_pricing import (
    expression_attribute_keys,
    expression_usage_keys,
    freeze_expression_context,
    validate_expression_policy,
)
from .expression_usage import request_usage_evidence
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
from .request_tracing import bind_call_id, bind_retail_request_id, current_trace, record_event, trace_phase
from .settlement_store import frozen_pricing, record_facts
from .settlement_stream import SettlementStreamFacts
from .settlement_worker import (
    notify_settlement,
    register_request_activity,
    unregister_request_activity,
)
from .store import WalletStore, get_store
from .trace_store import persist_trace

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
_active_pricing: Final[ContextVar[tuple[str, Mapping[str, object]] | None]] = ContextVar("retail_active_pricing", default=None)
_EXPRESSION_MEDIA: Final = frozenset({
    "image_generation", "aimage_generation", "image_edit", "aimage_edit", "transcription", "atranscription",
    "speech", "aspeech",
})


def reservation_id(data: Mapping[str, object]) -> str | None:
    containers: Final = (data, as_mapping(data.get("litellm_params")), as_mapping(data.get("standard_logging_object")))
    for container in containers:
        for key in ("metadata", "litellm_metadata"):
            value: Final = as_mapping(container.get(key)).get("retail_request_id")
            if isinstance(value, str) and value:
                return value
    return None


def _known_provider_cost(value: object) -> str | None:
    if value is None:
        return None
    try:
        number: Final = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return str(number) if number.is_finite() and number >= 0 else None


async def _persist_billing_trace(store: WalletStore | None) -> None:
    trace: Final = current_trace()
    if trace is None:
        return
    if store is None:
        from .trace_writer import queue_trace

        queue_trace(trace.snapshot(), retail_request_id=trace.retail_request_id)
        return
    try:
        await asyncio.wait_for(
            persist_trace(trace.snapshot(), retail_request_id=trace.retail_request_id, store=store), timeout=2,
        )
    except Exception as exc:
        verbose_proxy_logger.warning("Billing trace could not be saved (%s)", type(exc).__name__)


def _trace_resource_pool(pool: PoolRouteDecision | None) -> None:
    if pool is None:
        return
    attributes: Final = {
        "resource_pool_id": pool.pool_id, "resource_pool_name": pool.pool_name,
        "resource_source_kind": pool.source_kind, "routing_settings_version": pool.settings_version,
    }
    record_event("resource_pool.selected", attributes)
    trace: Final = current_trace()
    if trace is not None:
        trace.set_attributes(attributes)


def _request_output_limit(snapshot: Mapping[str, object], data: Mapping[str, object], call_type: str) -> int:
    limit_key: Final = "max_output_tokens" if call_type in {"responses", "aresponses"} else "max_tokens"
    provided: Final = tuple(
        data[key] for key in ("max_tokens", "max_completion_tokens", "max_output_tokens") if data.get(key) is not None
    )
    if len(provided) > 1:
        raise HTTPException(400, "Send only one output token limit")
    if (
        limit_key == "max_output_tokens"
        and any(data.get(key) is not None for key in ("max_tokens", "max_completion_tokens"))
    ) or (limit_key == "max_tokens" and data.get("max_output_tokens") is not None):
        raise HTTPException(400, "This output token limit is not valid for the requested endpoint")
    capacity: Final = (
        int(as_mapping(snapshot.get("expression_limits")).get("output_tokens", 1_000_000))
        if snapshot.get("version") == "expression-v1" else 1_000_000
    )
    requested: Final = provided[0] if provided else min(1024, capacity)
    if not isinstance(requested, int) or isinstance(requested, bool) or not 1 <= requested <= 1_000_000:
        raise HTTPException(400, "Output token limit must be an integer between 1 and 1000000")
    if requested > capacity:
        raise HTTPException(400, "Requested output token limit exceeds the frozen expression capacity")
    if call_type in {"embedding", "aembedding"}:
        return 0
    return capacity if call_type in _EXPRESSION_MEDIA else requested


def price_snapshot(
    card: Mapping[str, JsonValue],
    discount: Mapping[str, JsonValue],
    call_type: str,
    pool: PoolRouteDecision | None = None,
    request_data: Mapping[str, object] | None = None,
    started_at: datetime | None = None,
    routing_attributes: Mapping[str, str] | None = None,
) -> dict[str, JsonValue]:
    policy: Final = validate_expression_policy(card["expression_policy"]) if card.get("expression_policy") else None
    required_routing: Final = (
        expression_attribute_keys(policy).intersection({"data_residency", "vertex_location"}) if policy else frozenset()
    )
    if routing_attributes is not None and required_routing.difference(routing_attributes):
        raise ValueError(
            "Configured deployments do not share a verified pricing region. Use separate model prices for each region"
        )
    expression_context: Final = (
        freeze_expression_context(policy, request_data or {}, started_at or datetime.now(timezone.utc)) if policy else {}
    )
    return {
        **card,
        "version": "expression-v1" if policy else "token-v3",
        "price_version": card.get("version"),
        "multiplier": str(discount["discount"]),
        "discount": str(discount["discount"]),
        "discount_source": discount["discount_source"],
        "discount_model": discount["model"],
        "plan_id": discount["plan_id"],
        "billing_mode": "embedding" if call_type in {"embedding", "aembedding"} else "media" if call_type in _EXPRESSION_MEDIA else "text",
        **({
            "expression_policy": policy.model_dump(mode="json"),
            "expression_context": expression_context,
            "expression_usage_evidence": request_usage_evidence(request_data or {}, call_type),
            "expression_limits": expression_token_limits(card),
            "expression_routing_attributes": dict(routing_attributes or {}),
        } if policy else {}),
        **(
            {
                "resource_pool_id": pool.pool_id,
                "resource_pool_name": pool.pool_name,
                "resource_source_kind": pool.source_kind,
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
        with trace_phase("resource_pool.resolve"):
            pool: Final = await self._pools().resolve_route(user_id, model, policy, self.deployments())
        bind_pool_routing(model, pool, data)
        _trace_resource_pool(pool)
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
        call_id: Final = data.get("litellm_call_id")
        if isinstance(call_id, str):
            bind_call_id(call_id)
        with trace_phase("billing.prepare"):
            try:
                with fail_after(database_setting("RETAIL_PREFLIGHT_TIMEOUT_SECONDS", 10, 0.1, 300)):
                    prepared: Final = await self._prepare_request(user_api_key_dict, data, call_type)
            except (PoolTimeout, TooManyRequests, OperationalError, QueryCanceled, LockNotAvailable, TimeoutError) as exc:
                raise HTTPException(503, "Wallet service is busy. Retry this request shortly") from exc
            return data if prepared is data else dict(prepared)

    async def _prepare_request(
        self, user_api_key_dict: UserAPIKeyAuth, data: Mapping[str, object], call_type: str,
    ) -> Mapping[str, object]:
        _active_reservation.set(None)
        reset_pool_routing()
        policy: Final = _resource_policy(user_api_key_dict)
        exempt: Final = (
            str(user_api_key_dict.user_role) in {"proxy_admin", "LitellmUserRoles.PROXY_ADMIN"}
            or os.getenv("RETAIL_BILLING_ENABLED", "false").lower() != "true"
        )
        if exempt:
            record_event("billing.bypassed", {"reason": "administrator_or_billing_disabled"})
            return data if policy is None else await self._route_exempt_request(user_api_key_dict, data, policy)
        user_id: Final = user_api_key_dict.user_id
        if not user_id:
            raise HTTPException(403, "Wallet billing requires an API key owned by a user")
        if call_type not in _SUPPORTED and call_type not in _EXPRESSION_MEDIA:
            raise HTTPException(403, "This API endpoint is not enabled for prepaid wallets yet")
        if data.get("background") or data.get("batch") or data.get("service_tier") in ("batch", "batches"):
            raise HTTPException(400, "Background and batch requests are not enabled for prepaid wallets yet")
        image_call: Final = call_type in {"image_generation", "aimage_generation", "image_edit", "aimage_edit"}
        if image_call and (
            not isinstance(data.get("n", 1), int) or isinstance(data.get("n", 1), bool)
            or not 1 <= data.get("n", 1) <= 16
        ):
            raise HTTPException(400, "Image count must be an integer between 1 and 16")
        if any(
            data.get(key) is not None
            and (not isinstance(data[key], int) or isinstance(data[key], bool) or data[key] != 1)
            for key in (("best_of",) if image_call else ("n", "best_of"))
        ):
            raise HTTPException(400, "Prepaid requests currently support one generated candidate only")
        prompt: Final = data.get("prompt")
        if isinstance(prompt, list) and len(prompt) > 1 and any(isinstance(item, (str, list)) for item in prompt):
            raise HTTPException(400, "Send one text completion prompt per prepaid request")
        tools: Final = data.get("tools")
        priced_tools: Final = tuple(
            as_mapping(tool) for tool in tools if as_mapping(tool).get("type") not in {"function", "mcp"}
        ) if isinstance(tools, list) else ()
        if priced_tools and not all(
            item.get("type") in {"web_search", "web_search_preview", "google_search"}
            or "googleSearch" in item or "google_search" in item for item in priced_tools
        ):
            raise HTTPException(400, "This server-priced tool has no supported reported billing quantity")
        store: Final = self._store()
        from litellm.proxy.proxy_server import llm_router

        model: Final = str(data.get("model", ""))
        deployments: Final = llm_router.get_model_list(model, team_id=user_api_key_dict.team_id) if llm_router else ()
        if not deployments:
            raise HTTPException(400, "No configured deployment is available for this model")
        await store.initialize()
        with trace_phase("resource_pool.resolve"):
            pool: Final = await self._pools().resolve_route(user_id, model, policy, deployments)
        bind_pool_routing(model, pool, data)
        _trace_resource_pool(pool)
        priced_deployments: Final = (
            tuple(
                deployment
                for deployment in deployments
                if str(as_mapping(deployment.get("model_info")).get("id")) in pool.allowed_deployment_ids
            )
            if pool is not None
            else deployments
        )
        context: Final = await store.get_billing_context(user_id, model, pool.pool_id if pool is not None else None)
        price: Final = as_mapping(context.get("price"))
        try:
            initial: Final = None if price else catalog_snapshot(model, priced_deployments, "1")
            card: Final = price or await store.get_model_price(model, initial)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if card is None:
            raise HTTPException(422, "A model sales price is required")
        if priced_tools and (
            not card.get("expression_policy") or "web_search_requests" not in expression_usage_keys(
                validate_expression_policy(card["expression_policy"])
            )
        ):
            raise HTTPException(400, "Server-priced search requires an expression with reported web_search_requests")
        if call_type in _EXPRESSION_MEDIA and not card.get("expression_policy"):
            raise HTTPException(422, "This media endpoint requires an explicit expression price with reported billing quantities")
        discount: Final = as_mapping(context["discount"])
        try:
            routing_keys: Final = (
                expression_attribute_keys(validate_expression_policy(card["expression_policy"]))
                if card.get("expression_policy") else frozenset()
            )
            snapshot: Final = price_snapshot(
                card, discount, call_type, pool, data,
                routing_attributes=catalog_routing_attributes(priced_deployments, routing_keys),
            )
            if call_type in _EXPRESSION_MEDIA and snapshot["version"] != "expression-v1":
                raise ValueError("This media endpoint requires an explicit expression price with reported billing quantities")
            if priced_tools and (
                snapshot["version"] != "expression-v1" or "web_search_requests" not in expression_usage_keys(
                    validate_expression_policy(snapshot["expression_policy"])
                )
            ):
                raise ValueError("Server-priced search requires an expression with reported web_search_requests")
            if image_call and snapshot["version"] == "expression-v1":
                policy: Final = validate_expression_policy(snapshot["expression_policy"])
                image_bound: Final = policy.usage_bounds.get("images", policy.usage_bounds.get("image_count"))
                if image_bound is not None and data.get("n", 1) > Decimal(image_bound):
                    raise ValueError("Requested image count exceeds the expression's configured usage bound")
            validate_requested_cache_ttls(snapshot, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        embedding: Final = call_type in {"embedding", "aembedding"}
        limit_key: Final = "max_output_tokens" if call_type in {"responses", "aresponses"} else "max_tokens"
        output_limit: Final = _request_output_limit(snapshot, data, call_type)
        request_id: Final = str(uuid4())
        opaque_payload: Final = json.dumps({key: data.get(key) for key in ("messages", "input", "tools")}, default=str)
        opaque_input: Final = bool(priced_tools) or any(
            data.get(key) for key in ("previous_response_id", "conversation", "file", "attachments")
        ) or any(
            marker in opaque_payload
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
        try:
            quote: Final = reserve_quote(
                snapshot, data, output_limit, minimum_input_tokens=1_000_000 if opaque_input else 0
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            with trace_phase("billing.reserve"):
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
        _active_pricing.set((request_id, snapshot))
        register_request_activity(request_id, self.store)
        bind_retail_request_id(request_id)
        record_event("billing.reserved", {
            "retail_request_id": request_id, "amount": quote,
            "price_version": str(snapshot.get("price_version", "")),
            "billing_mode": str(snapshot.get("billing_mode", "")),
        })
        limit: Final = {} if embedding or call_type in _EXPRESSION_MEDIA or data.get("max_completion_tokens") is not None else {limit_key: output_limit}
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
        bind_retail_request_id(request_id)
        try:
            with trace_phase("billing.persist_facts"):
                await self._settle_reservation(data, response, provider_cost, request_id)
        finally:
            await _persist_billing_trace(self.store)

    async def _settle_reservation(
        self, data: Mapping[str, object], response: object, provider_cost: object, request_id: str,
    ) -> None:
        store: Final = self._store()
        active: Final = _active_pricing.get()
        cached: Final = active is not None and active[0] == request_id
        saved: Final = None if cached else await frozen_pricing(store, request_id)
        if not cached and saved is None:
            return
        snapshot: Final = active[1] if cached and active is not None else as_mapping(saved)
        cost_value: Final = _known_provider_cost(provider_cost)
        if snapshot.get("version") == "expression-v1":
            try:
                expression_amount, expression_details = expression_settlement(snapshot, response)
            except ValueError as exc:
                await record_facts(store, request_id, None, {"billing_error": type(exc).__name__})
                unregister_request_activity(request_id)
                record_event("billing.pending", {"reason": "expression_usage_incomplete"})
                return
            await record_facts(store, request_id, expression_amount, {
                **expression_details,
                "provider_cost": cost_value,
                "gateway_call_id": str(data.get("litellm_call_id", "")),
            })
            unregister_request_activity(request_id)
            notify_settlement()
            record_event("billing.facts_committed", {"retail_request_id": request_id, "amount": expression_amount})
            return
        facts: Final = usage_facts(response, allow_input_only=snapshot.get("billing_mode") == "embedding")
        if facts is None:
            await record_facts(store,
                request_id, None, {"billing_error": "Reported token usage is missing, incomplete or inconsistent"}
            )
            unregister_request_activity(request_id)
            record_event("billing.pending", {"reason": "reported_usage_incomplete"})
            return
        try:
            amount: Final = charge_for_usage(snapshot, facts)
            breakdown: Final = cost_breakdown(snapshot, facts)
        except ValueError as exc:
            await record_facts(store, request_id, None, {"usage": facts.model_dump(), "billing_error": type(exc).__name__})
            unregister_request_activity(request_id)
            record_event("billing.pending", {"reason": "reported_usage_inconsistent"})
            return
        details: Final[dict[str, JsonValue]] = {
            "usage": facts.model_dump(),
            "cost_breakdown": breakdown,
            "provider_cost": cost_value,
            "gateway_call_id": str(data.get("litellm_call_id", "")),
            "billing_basis": "reported_usage",
        }
        await record_facts(store, request_id, amount, details)
        unregister_request_activity(request_id)
        notify_settlement()
        record_event("billing.facts_committed", {"retail_request_id": request_id, "amount": amount})

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: UserAPIKeyAuth, response: AsyncIterator[object], request_data: Mapping[str, object],
    ) -> AsyncIterator[object]:
        if reservation_id(request_data) is None:
            async for chunk in response:
                yield chunk
            return
        observed: Final = SettlementStreamFacts()
        async for chunk in response:
            terminal: Final = observed.feed(chunk)
            if terminal is not None:
                await self._settle(request_data, terminal)
            yield chunk
        if not observed.emitted:
            logging: Final = request_data.get("litellm_logging_obj")
            reported: Final = as_mapping(getattr(logging, "model_call_details", {})).get("retail_reported_usage")
            final: Final = observed.finish()
            await self._settle(request_data, {**final, "usage": reported} if isinstance(reported, Mapping) else final)

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
        if kwargs.get("stream") or kwargs.get("complete_streaming_response") is not None:
            return
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
        try:
            with trace_phase("billing.failure"):
                await self._handle_failure(request_data, original_exception)
        finally:
            await _persist_billing_trace(self.store)

    async def _handle_failure(self, request_data: Mapping[str, object], original_exception: Exception) -> None:
        request_id: Final = _active_reservation.get()
        if request_id is None:
            return
        store: Final = self._store()
        reservation: Final = await store.get_reservation(request_id)
        if reservation is None or reservation["status"] != "reserved":
            return
        if pool_rejected_before_upstream():
            await store.release(request_id, "No eligible resource-pool deployment was contacted")
            record_event("billing.released", {"retail_request_id": request_id, "reason": "no_upstream_contacted"})
            return
        if pool_has_uncertain_attempt():
            record_event("billing.pending", {"retail_request_id": request_id, "reason": "uncertain_upstream_attempt"})
            return
        # An interrupted stream or an accounting error is not proof that the provider used nothing.
        if request_data.get("stream") or not isinstance(original_exception, Exception):
            record_event("billing.pending", {"retail_request_id": request_id, "reason": "interrupted_or_streaming_request"})
            return
        status_code: Final = getattr(original_exception, "status_code", None)
        if status_code in {400, 401, 403, 404, 413, 422, 429}:
            await store.release(request_id, "Request rejected without billable usage")
            record_event("billing.released", {"retail_request_id": request_id, "reason": "rejected_without_billable_usage"})


wallet_callback: Final = RetailBilling()
