import json
import os
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import datetime
from typing import Final
from uuid import uuid4

from fastapi import HTTPException
from pydantic import JsonValue

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

from .models import InsufficientFunds, ParallelLimitExceeded
from .pricing import as_mapping, catalog_snapshot, charge_for_usage, reserve_quote, usage_facts
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


class RetailBilling(CustomLogger):
    is_accounting_callback = True

    def __init__(self, store: WalletStore | None = None) -> None:
        super().__init__()
        self.store = store

    def _store(self) -> WalletStore:
        return self.store if self.store is not None else get_store()

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: object,
        data: dict[str, object],
        call_type: str,
    ) -> dict[str, object]:
        _active_reservation.set(None)
        if str(user_api_key_dict.user_role) in {"proxy_admin", "LitellmUserRoles.PROXY_ADMIN"}:
            return data
        if os.getenv("RETAIL_BILLING_ENABLED", "false").lower() != "true":
            return data
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
        wallet: Final = await store.get_wallet(user_id, create=True)
        from litellm.proxy.proxy_server import llm_router

        model: Final = str(data.get("model", ""))
        deployments: Final = llm_router.get_model_list(model, team_id=user_api_key_dict.team_id) if llm_router else ()
        if not deployments:
            raise HTTPException(400, "No configured deployment is available for this model")
        price: Final = await store.get_model_price(model)
        try:
            initial: Final = None if price else catalog_snapshot(model, deployments, "1")
            card: Final = price or await store.get_model_price(model, initial)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if card is None:
            raise HTTPException(422, "A model sales price is required")
        snapshot: Final = {
            **card,
            "version": "flat-token-v1",
            "price_version": card.get("version"),
            "multiplier": str(as_mapping(wallet["pricing_plan"])["multiplier"]),
            "plan_id": as_mapping(wallet["pricing_plan"])["id"],
            "billing_mode": "embedding" if call_type in {"embedding", "aembedding"} else "text",
        }
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
        metadata: Final = {**as_mapping(data.get("metadata")), "retail_request_id": request_id}
        litellm_metadata: Final = {**as_mapping(data.get("litellm_metadata")), "retail_request_id": request_id}
        limit: Final = {} if embedding or "max_completion_tokens" in data else {limit_key: output_limit}
        return {**data, **limit, "metadata": metadata, "litellm_metadata": litellm_metadata}

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
            return
        amount: Final = charge_for_usage(snapshot, facts)
        details: Final[dict[str, JsonValue]] = {
            "usage": facts.model_dump(),
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
        # An interrupted stream or an accounting error is not proof that the provider used nothing.
        if request_data.get("stream") or not isinstance(original_exception, Exception):
            return
        status_code: Final = getattr(original_exception, "status_code", None)
        if status_code in {400, 401, 403, 404, 413, 422, 429}:
            await store.release(request_id, "Request rejected without billable usage")


wallet_callback: Final = RetailBilling()
