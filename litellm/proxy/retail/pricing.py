import json
import re
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import BaseModel, Field, JsonValue

from litellm.litellm_core_utils.get_model_cost_map import GetModelCostMap, get_model_cost_map_source_info

from .cache_usage import MAX_TTL_TIERS, CacheUsage, cache_read_count, cache_usage, token_count, ttl_seconds_key
from .models import cost_to_nanos, nanos_to_amount


class UsageFacts(CacheUsage):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)


def as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, Mapping):
        return value
    return {}


def response_usage(response: object) -> Mapping[str, object]:
    payload: Final = as_mapping(response)
    usage: Final = as_mapping(payload.get("usage") or payload.get("usageMetadata"))
    choices: Final = payload.get("choices")
    choice_values: Final = tuple(as_mapping(choice) for choice in choices) if isinstance(choices, (tuple, list)) else ()
    supplemental: Final = (
        *(as_mapping(container.get("usage")) for choice in choice_values
          for container in (choice, as_mapping(choice.get("provider_specific_fields")))),
        {"cached_tokens": as_mapping(payload.get("timings")).get("cache_n")},
        {"cached_tokens": as_mapping(as_mapping(payload.get("provider_specific_fields")).get("timings")).get("cache_n")},
    )
    reads: Final = tuple(cache_read_count(item) for item in supplemental if any(v is not None for v in item.values()))
    return {**usage, "cached_tokens": max(cache_read_count(usage), *reads)} if reads else usage


def usage_facts(response: object, *, allow_input_only: bool = False) -> UsageFacts | None:
    try:
        return _response_usage_facts(response_usage(response), allow_input_only)
    except ValueError:
        return None


def _response_usage_facts(usage: Mapping[str, object], allow_input_only: bool) -> UsageFacts | None:
    if "promptTokenCount" in usage:
        return _gemini_usage(usage, allow_input_only)
    if not usage:
        return None
    if not any(key in usage for key in ("prompt_tokens", "input_tokens")):
        return None
    if not allow_input_only and not any(key in usage for key in ("completion_tokens", "output_tokens")):
        return None
    return _valid_usage(usage)


def _valid_usage(usage: Mapping[str, object]) -> UsageFacts:
    cache: Final = cache_usage(usage)
    aggregate: Final = "prompt_tokens" in usage or "input_tokens_details" in usage or "input_details" in usage
    prompt: Final = token_count(usage.get("prompt_tokens", usage.get("input_tokens")))
    cached_input: Final = cache.cache_read_tokens + cache.cache_write_tokens
    if aggregate and cached_input > prompt:
        raise ValueError("Cached input cannot exceed the reported aggregate input")
    return UsageFacts(
        input_tokens=prompt - cached_input if aggregate else prompt,
        output_tokens=token_count(usage.get("completion_tokens", usage.get("output_tokens", 0))),
        cache_read_tokens=cache.cache_read_tokens,
        cache_write_tokens=cache.cache_write_tokens,
        cache_write_ttl_tokens=cache.cache_write_ttl_tokens,
    )


def _gemini_usage(usage: Mapping[str, object], allow_input_only: bool) -> UsageFacts | None:
    if "promptTokenCount" not in usage or (not allow_input_only and "candidatesTokenCount" not in usage):
        return None
    prompt: Final = token_count(usage["promptTokenCount"]) + token_count(usage.get("toolUsePromptTokenCount", 0))
    read: Final = token_count(usage.get("cachedContentTokenCount", 0))
    if read > prompt:
        raise ValueError("Cached input cannot exceed the reported aggregate input")
    return UsageFacts(
        input_tokens=prompt - read,
        output_tokens=token_count(usage.get("candidatesTokenCount", 0)) + token_count(usage.get("thoughtsTokenCount", 0)),
        cache_read_tokens=read,
    )


def validated_cache_write_tiers(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > MAX_TTL_TIERS:
        raise ValueError("Cache write prices must contain at most sixteen duration tiers")
    return {ttl_seconds_key(key): _validated_tier_price(rate) for key, rate in value.items()}


def _validated_tier_price(value: object) -> str:
    rate: Final = _rate(value)
    if rate is None or rate > 1_000_000:
        raise ValueError("Cache write prices must be finite non-negative decimal USD values")
    try:
        if rate.quantize(Decimal("0.000000000000000001")) != rate:
            raise ValueError("Cache write prices must have at most eighteen decimals")
    except InvalidOperation as exc:
        raise ValueError("Cache write prices must have at most eighteen decimals") from exc
    return format(rate, "f")


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


def catalog_cache_write_tiers(name: str, rates: Mapping[str, object]) -> dict[str, str]:
    supplied: Final = rates.get("cache_write_tiers")
    if supplied is not None:
        return validated_cache_write_tiers(supplied)
    provider: Final = str(rates.get("litellm_provider", ""))
    anthropic: Final = provider == "anthropic" or name.startswith("anthropic/") or (
        "claude" in name and provider in {"bedrock", "vertex_ai", "vertex_ai-anthropic_models"}
    )
    openai: Final = provider == "openai" or name.startswith("openai/")
    if not anthropic and not openai:
        return {}
    base: Final = rates.get("cache_creation_input_token_cost")
    duration_key: Final = (
        "cache_creation_input_token_cost_above_1hr" if anthropic else "cache_creation_input_token_cost_above_12hr"
    )
    long: Final = rates.get(duration_key)
    return validated_cache_write_tiers(
        {
            **({"300" if anthropic else "1800": base} if base is not None else {}),
            **({"3600" if anthropic else "43200": long} if long is not None else {}),
        }
    )


def _catalog_card(name: str, rates: Mapping[str, object]) -> dict[str, JsonValue] | None:
    try:
        tiers: Final = catalog_cache_write_tiers(name, rates)
    except ValueError:
        return None
    supported_rates: Final = {
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
        *(("cache_creation_input_token_cost_above_1hr",) if "3600" in tiers else ()),
        *(("cache_creation_input_token_cost_above_12hr",) if "43200" in tiers else ()),
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
        "cache_write_tiers": dict(tiers),
        "cache_write_price_published": write is not None,
    }


def _published_reference_card(
    card: Mapping[str, object], catalog: Mapping[str, Mapping[str, object]],
) -> Mapping[str, object]:
    if card.get("cache_write_price_published") is not None or card.get("source") != "catalog":
        return card
    name: Final = card.get("catalog_model")
    reference: Final = catalog.get(name, {}) if isinstance(name, str) else {}
    return {**card, "cache_write_price_published": reference.get("cache_creation_input_token_cost") is not None}


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
            "A single supported catalog reference is required. Import or configure the model's base price in Model Pricing"
        )
    return {"version": "token-v3", "multiplier": multiplier, **candidates[0]}


def _cache_write_cost(snapshot: Mapping[str, object], usage: UsageFacts) -> Decimal:
    if snapshot.get("version") not in {"token-v2", "token-v3"}:
        return Decimal(str(snapshot["cache_creation_input_token_cost"])) * usage.cache_write_tokens
    tiers: Final = validated_cache_write_tiers(snapshot.get("cache_write_tiers", {}))
    missing: Final = tuple(key for key in usage.cache_write_ttl_tokens if key not in tiers)
    if missing and snapshot.get("version") == "token-v2":
        raise ValueError("Cache write prices are missing for TTL seconds: " + ", ".join(missing))
    default: Final = _default_cache_write_rate(snapshot)
    return (
        default * usage.cache_write_unclassified_tokens
        + sum((Decimal(tiers[key]) * tokens if key in tiers else default * tokens
               for key, tokens in usage.cache_write_ttl_tokens.items()), Decimal(0))
    )


def _default_cache_write_rate(snapshot: Mapping[str, object]) -> Decimal:
    rate: Final = _rate(snapshot.get("cache_creation_input_token_cost"))
    if rate is None:
        raise ValueError("A finite non-negative default cache write price is required")
    return rate


def _usage_amount(snapshot: Mapping[str, object], usage: UsageFacts) -> Decimal:
    return (
        Decimal(str(snapshot["input_cost_per_token"])) * usage.input_tokens
        + Decimal(str(snapshot["output_cost_per_token"])) * usage.output_tokens
        + Decimal(str(snapshot["cache_read_input_token_cost"])) * usage.cache_read_tokens
        + _cache_write_cost(snapshot, usage)
    ) * Decimal(str(snapshot.get("discount", snapshot.get("multiplier"))))


def charge_for_usage(snapshot: Mapping[str, object], usage: UsageFacts) -> str:
    return nanos_to_amount(cost_to_nanos(_usage_amount(snapshot, usage)))


def cost_breakdown(snapshot: Mapping[str, object], usage: UsageFacts) -> dict[str, JsonValue]:
    discount: Final = Decimal(str(snapshot.get("discount", snapshot.get("multiplier"))))
    tiers: Final = validated_cache_write_tiers(snapshot.get("cache_write_tiers", {}))
    current: Final = snapshot.get("version") in {"token-v2", "token-v3"}
    _cache_write_cost(snapshot, usage)
    return {
        "input": format(Decimal(str(snapshot["input_cost_per_token"])) * usage.input_tokens * discount, "f"),
        "output": format(Decimal(str(snapshot["output_cost_per_token"])) * usage.output_tokens * discount, "f"),
        "cache_read": format(
            Decimal(str(snapshot["cache_read_input_token_cost"])) * usage.cache_read_tokens * discount, "f"
        ),
        "cache_write_unclassified": format(
            Decimal(str(snapshot["cache_creation_input_token_cost"]))
            * (usage.cache_write_unclassified_tokens if current else usage.cache_write_tokens) * discount,
            "f",
        ),
        "cache_write_tiers": {
            key: format(Decimal(tiers.get(key, snapshot["cache_creation_input_token_cost"])) * count * discount, "f")
            for key, count in usage.cache_write_ttl_tokens.items() if current
        },
        "rounding_adjustment": format(
            Decimal(charge_for_usage(snapshot, usage)) - _usage_amount(snapshot, usage), "f"
        ),
    }


def validate_requested_cache_ttls(snapshot: Mapping[str, object], data: Mapping[str, object]) -> None:
    if snapshot.get("version") == "token-v3":
        _default_cache_write_rate(snapshot)
        tuple(_requested_cache_ttls(data))
        return
    if snapshot.get("version") != "token-v2":
        return
    tiers: Final = validated_cache_write_tiers(snapshot.get("cache_write_tiers", {}))
    missing: Final = tuple(sorted(frozenset(_requested_cache_ttls(data)) - tiers.keys(), key=int))
    if missing:
        raise ValueError("Configure cache write prices for TTL seconds before sending this request: " + ", ".join(missing))


def _requested_cache_ttls(value: object) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"cache_control", "prompt_cache_options", "prompt_cache_breakpoint"}:
                control: Final = as_mapping(item)
                if control.get("mode") in {"off", "disabled", "read_only"}:
                    continue
                ttl: Final = control.get("ttl")
                if isinstance(ttl, str) and (match := re.fullmatch(r"(\d+)([smhd])", ttl)):
                    scale: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
                    yield ttl_seconds_key(str(int(match.group(1)) * scale))
            else:
                yield from _requested_cache_ttls(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _requested_cache_ttls(item)


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
        *(Decimal(value) for value in validated_cache_write_tiers(snapshot.get("cache_write_tiers", {})).values()),
    )
    amount: Final = (
        input_rate * upper_input + Decimal(str(snapshot["output_cost_per_token"])) * output_limit
    ) * Decimal(str(snapshot.get("discount", snapshot.get("multiplier"))))
    return nanos_to_amount(cost_to_nanos(amount))
