import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import BaseModel, ConfigDict, JsonValue

import litellm

from .models import cost_to_nanos, nanos_to_amount


class UsageFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, Mapping):
        return value
    return {}


def _count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValueError("A reported token count must be a non-negative integer")


def usage_facts(response: object, *, allow_input_only: bool = False) -> UsageFacts | None:
    payload: Final = as_mapping(response)
    usage: Final = as_mapping(payload.get("usage"))
    if not usage:
        return None
    if not any(key in usage for key in ("prompt_tokens", "input_tokens")):
        return None
    if not allow_input_only and not any(key in usage for key in ("completion_tokens", "output_tokens")):
        return None
    try:
        return _valid_usage(usage)
    except ValueError:
        return None


def _valid_usage(usage: Mapping[str, object]) -> UsageFacts:
    read: Final = _count(usage.get("cache_read_input_tokens") or 0)
    write: Final = _count(usage.get("cache_creation_input_tokens") or 0)
    details: Final = as_mapping(usage.get("prompt_tokens_details") or usage.get("input_tokens_details"))
    cached: Final = max(read, _count(details.get("cached_tokens") or 0))
    created: Final = max(write, _count(details.get("cache_creation_tokens") or 0))
    aggregate: Final = "prompt_tokens" in usage or "input_tokens_details" in usage
    prompt: Final = _count(usage.get("prompt_tokens", usage.get("input_tokens")))
    if aggregate and cached + created > prompt:
        raise ValueError("Cached input cannot exceed the reported aggregate input")
    return UsageFacts(
        input_tokens=max(0, prompt - cached - created) if aggregate else prompt,
        output_tokens=_count(usage.get("completion_tokens", usage.get("output_tokens", 0))),
        cache_read_tokens=cached,
        cache_write_tokens=created,
    )


def _rate(value: object) -> Decimal | None:
    try:
        rate: Final = Decimal(str(value))
        return rate if rate.is_finite() and rate >= 0 else None
    except InvalidOperation:
        return None


def catalog_snapshot(model: str, deployments: Sequence[Mapping[str, object]], multiplier: str) -> dict[str, JsonValue]:
    cards: Final = tuple(_deployment_card(deployment) for deployment in deployments)
    valid: Final = tuple(card for card in cards if card is not None)
    if not valid:
        raise ValueError("This model needs explicit input and output token prices before wallet billing can be enabled")
    return {
        "version": "flat-token-v1",
        "model": model,
        "multiplier": multiplier,
        "input_cost_per_token": str(max(card["input_cost_per_token"] for card in valid)),
        "output_cost_per_token": str(max(card["output_cost_per_token"] for card in valid)),
        "cache_read_input_token_cost": str(max(card["cache_read_input_token_cost"] for card in valid)),
        "cache_creation_input_token_cost": str(max(card["cache_creation_input_token_cost"] for card in valid)),
    }


def _deployment_card(deployment: Mapping[str, object]) -> dict[str, Decimal] | None:
    params: Final = as_mapping(deployment.get("litellm_params"))
    info: Final = as_mapping(deployment.get("model_info"))
    native: Final = _native_card(str(params.get("model", "")))
    source: Final = {**native, **{key: value for key, value in info.items() if value is not None}}
    rates: Final = {**source, **{key: value for key, value in params.items() if value is not None}}
    input_rate: Final = _rate(rates.get("input_cost_per_token"))
    output_rate: Final = _rate(rates.get("output_cost_per_token", 0 if rates.get("mode") == "embedding" else None))
    if input_rate is None or output_rate is None:
        return None
    return {
        "input_cost_per_token": input_rate,
        "output_cost_per_token": output_rate,
        "cache_read_input_token_cost": _optional_rate(rates.get("cache_read_input_token_cost"), input_rate),
        "cache_creation_input_token_cost": _optional_rate(rates.get("cache_creation_input_token_cost"), input_rate),
    }


def _native_card(model: str) -> Mapping[str, object]:
    try:
        return as_mapping(litellm.get_model_info(model=model))
    except Exception:
        return {}


def _optional_rate(value: object, fallback: Decimal) -> Decimal:
    rate: Final = _rate(value)
    return fallback if rate is None else rate


def charge_for_usage(snapshot: Mapping[str, object], usage: UsageFacts) -> str:
    amount: Final = (
        Decimal(str(snapshot["input_cost_per_token"])) * usage.input_tokens
        + Decimal(str(snapshot["output_cost_per_token"])) * usage.output_tokens
        + Decimal(str(snapshot["cache_read_input_token_cost"])) * usage.cache_read_tokens
        + Decimal(str(snapshot["cache_creation_input_token_cost"])) * usage.cache_write_tokens
    ) * Decimal(str(snapshot["multiplier"]))
    return nanos_to_amount(cost_to_nanos(amount))


def reserve_quote(
    snapshot: Mapping[str, object],
    data: Mapping[str, object],
    output_limit: int,
    minimum_input_tokens: int = 0,
) -> str:
    content: Final = {
        key: data[key] for key in ("messages", "prompt", "input", "system", "tools", "instructions") if key in data
    }
    upper_input: Final = max(
        minimum_input_tokens, len(json.dumps(content, ensure_ascii=False, default=str).encode("utf-8")) + 1024
    )
    input_rate: Final = max(
        Decimal(str(snapshot["input_cost_per_token"])),
        Decimal(str(snapshot["cache_read_input_token_cost"])),
        Decimal(str(snapshot["cache_creation_input_token_cost"])),
    )
    amount: Final = (
        input_rate * upper_input + Decimal(str(snapshot["output_cost_per_token"])) * output_limit
    ) * Decimal(str(snapshot["multiplier"]))
    return nanos_to_amount(cost_to_nanos(amount))
