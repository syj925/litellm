import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import BaseModel, ConfigDict, JsonValue

from litellm.litellm_core_utils.get_model_cost_map import GetModelCostMap, get_model_cost_map_source_info

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


def catalog_candidates(
    model: str,
    deployments: Sequence[Mapping[str, object]],
    catalog: Mapping[str, Mapping[str, object]] | None = None,
    source_label: str | None = None,
) -> tuple[dict[str, JsonValue], ...]:
    source: Final = GetModelCostMap.loaded_model_cost_map() if catalog is None else catalog
    label: Final = source_label or catalog_source_label()
    names: Final = tuple(_catalog_model(deployment, source) for deployment in deployments)
    unique_names: Final = tuple(dict.fromkeys(name for name in names if name is not None))
    cards: Final = tuple(_catalog_card(name, source[name]) for name in unique_names)
    return tuple({"model": model, "source": "catalog", "source_label": label, **card} for card in cards if card)


def catalog_source_label() -> str:
    provenance: Final = get_model_cost_map_source_info()
    revision: Final = provenance["source_revision"] or "unknown revision"
    return f"LiteLLM reference catalog ({revision})"


def _catalog_model(
    deployment: Mapping[str, object],
    catalog: Mapping[str, Mapping[str, object]],
) -> str | None:
    params: Final = as_mapping(deployment.get("litellm_params"))
    info: Final = as_mapping(deployment.get("model_info"))
    name: Final = str(info.get("base_model") or params.get("model", ""))
    if name in catalog:
        return name
    suffix: Final = name.partition("/")[2]
    return suffix if suffix and suffix in catalog else None


def _catalog_card(name: str, rates: Mapping[str, object]) -> dict[str, JsonValue] | None:
    supported_rates: Final = {
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
    }
    if rates.get("mode") not in {None, "chat", "completion", "embedding"}:
        return None
    if any(
        "cost" in key
        and key not in supported_rates
        and value is not None
        and any(marker in key for marker in ("above_", "below_", "tier"))
        for key, value in rates.items()
    ):
        return None
    input_rate: Final = _rate(rates.get("input_cost_per_token"))
    output_rate: Final = _rate(rates.get("output_cost_per_token", 0 if rates.get("mode") == "embedding" else None))
    if input_rate is None or output_rate is None:
        return None
    read: Final = rates.get("cache_read_input_token_cost")
    write: Final = rates.get("cache_creation_input_token_cost")
    read_rate: Final = input_rate if read is None else _rate(read)
    write_rate: Final = input_rate if write is None else _rate(write)
    if read_rate is None or write_rate is None:
        return None
    return {
        "catalog_model": name,
        "input_cost_per_token": str(input_rate),
        "output_cost_per_token": str(output_rate),
        "cache_read_input_token_cost": str(read_rate),
        "cache_creation_input_token_cost": str(write_rate),
    }


def catalog_snapshot(
    model: str,
    deployments: Sequence[Mapping[str, object]],
    multiplier: str,
    catalog: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, JsonValue]:
    source: Final = GetModelCostMap.loaded_model_cost_map() if catalog is None else catalog
    candidates: Final = catalog_candidates(model, deployments, source)
    if len(candidates) != 1 or any(
        _catalog_model(deployment, source) != candidates[0]["catalog_model"] for deployment in deployments
    ):
        raise ValueError(
            "A single supported catalog reference is required. Import the model's official reference price in Customer wallets"
        )
    return {"version": "flat-token-v1", "multiplier": multiplier, **candidates[0]}


def charge_for_usage(snapshot: Mapping[str, object], usage: UsageFacts) -> str:
    amount: Final = (
        Decimal(str(snapshot["input_cost_per_token"])) * usage.input_tokens
        + Decimal(str(snapshot["output_cost_per_token"])) * usage.output_tokens
        + Decimal(str(snapshot["cache_read_input_token_cost"])) * usage.cache_read_tokens
        + Decimal(str(snapshot["cache_creation_input_token_cost"])) * usage.cache_write_tokens
    ) * Decimal(str(snapshot.get("discount", snapshot.get("multiplier"))))
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
    ) * Decimal(str(snapshot.get("discount", snapshot.get("multiplier"))))
    return nanos_to_amount(cost_to_nanos(amount))
