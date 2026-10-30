import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import JsonValue

from .expression_pricing import validate_expression_policy

_BASE_FIELDS: Final = {
    "input_cost_per_token": "p",
    "output_cost_per_token": "c",
    "cache_read_input_token_cost": "cr",
    "cache_creation_input_token_cost": "cc",
    "cache_creation_input_token_cost_above_1hr": "cc1h",
    "cache_creation_input_token_cost_above_12hr": "cc12h",
    "input_cost_per_audio_token": "ai",
    "output_cost_per_audio_token": "ao",
    "input_cost_per_image_token": "img",
    "output_cost_per_image_token": "img_o",
    "cache_read_image_token_cost": "img_cr",
    "cache_read_input_image_token_cost": "img_cr",
    "output_cost_per_reasoning_token": "reasoning",
}
_CONTEXT: Final = re.compile(r"_above_(\d+(?:\.\d+)?)([km]?)_tokens")
_SERVICE: Final = re.compile(r"_(priority|flex|ultrafast|batches)$")
_PRICING_METADATA_MARKERS: Final = ("price", "pricing", "multiplier", "discount", "surcharge", "fee", "free_requests")
_MAPPED_PRICING_METADATA: Final = frozenset(("cache_write_price_published",))


def unsupported_catalog_pricing_metadata(metadata: Mapping[str, object]) -> bool:
    provider: Final = metadata.get("provider_specific_entry")
    if provider is not None and (
        not isinstance(provider, Mapping) or any(key != "bedrock_invocation_schema" for key in provider)
    ):
        return True
    if metadata.get("currency") not in (None, "USD"):
        return True
    if metadata.get("web_search_billing_unit") not in (None, "per_prompt", "per_query"):
        return True
    return any(
        value is not None and key not in _MAPPED_PRICING_METADATA
        and any(marker in key for marker in _PRICING_METADATA_MARKERS)
        for key, value in metadata.items()
    )


@dataclass(frozen=True, slots=True)
class _Rate:
    variable: str
    threshold: int
    service: str
    amount: Decimal


def _decimal(value: object) -> Decimal | None:
    try:
        number: Final = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or not 0 <= number <= 1_000_000 or (number != 0 and number.adjusted() < -36):
        return None
    return Decimal(0) if number == 0 else number


def _parse_rate(key: str, value: object) -> _Rate | None:
    if len(key) > 200:
        return None
    context: Final = _CONTEXT.search(key)
    without_context: Final = _CONTEXT.sub("", key)
    service: Final = _SERVICE.search(without_context)
    canonical: Final = _SERVICE.sub("", without_context)
    variable: Final = _BASE_FIELDS.get(canonical)
    amount: Final = _decimal(value)
    if variable is None or amount is None:
        return None
    return _Rate(
        variable=variable,
        threshold=int(Decimal(context.group(1)) * {"": 1, "k": 1000, "m": 1000000}[context.group(2)]) if context else 0,
        service=service.group(1) if service else "default",
        amount=amount * 1_000_000,
    )


def _pick(rates: tuple[_Rate, ...], variable: str, service: str, threshold: int) -> Decimal | None:
    choices: Final = tuple(
        rate
        for rate in rates
        if rate.variable == variable and rate.threshold <= threshold and rate.service in {"default", service}
    )
    selected: Final = max(choices, key=lambda rate: (rate.threshold, rate.service == service)) if choices else None
    return selected.amount if selected else None


def _fallback(rates: tuple[_Rate, ...], variable: str, service: str, threshold: int) -> Decimal:
    default_input: Final = _pick(rates, "p", service, threshold)
    parent: Final = (
        "cc"
        if variable.startswith("cc") or variable.startswith("cache_write(")
        else "cr"
        if variable == "img_cr"
        else "c"
        if variable in {"c", "img_o", "ao", "reasoning"}
        else "p"
    )
    inherited: Final = _pick(rates, parent, service, threshold)
    return inherited if inherited is not None else Decimal(0) if parent == "c" else default_input or Decimal(0)


def _formula(rates: tuple[_Rate, ...], service: str, threshold: int, variables: tuple[str, ...]) -> str:
    terms: Final = tuple((variable, _pick(rates, variable, service, threshold)) for variable in variables)
    return " + ".join(
        f"{variable} * {format(amount if amount is not None else _fallback(rates, variable, service, threshold), 'f')}"
        for variable, amount in terms
    )


def _contexts(rates: tuple[_Rate, ...], service: str, variables: tuple[str, ...]) -> str:
    thresholds: Final = tuple(sorted({rate.threshold for rate in rates if rate.threshold > 0}, reverse=True))
    branches: Final = tuple(
        f'len > {threshold} ? tier("{service}: above {threshold} input tokens", '
        f"{_formula(rates, service, threshold, variables)}) : "
        for threshold in thresholds
    )
    return "(" + "".join(branches) + f'tier("{service}: base", {_formula(rates, service, 0, variables)})' + ")"


def _explicit_cache_rates(metadata: Mapping[str, object]) -> tuple[_Rate, ...] | None:
    supplied: Final = metadata.get("cache_write_tiers", {})
    if not isinstance(supplied, Mapping) or len(supplied) > 16:
        return None
    pairs: Final = tuple((str(key), _decimal(value)) for key, value in supplied.items())
    if any(
        len(key) > 8
        or not key.isascii()
        or not key.isdecimal()
        or str(int(key)) != key
        or not 1 <= int(key) <= 31536000
        or value is None
        for key, value in pairs
    ):
        return None
    aliases: Final = {"300": "cc5m", "1800": "cc30m", "3600": "cc1h", "43200": "cc12h"}
    return tuple(
        _Rate(aliases.get(key, f'cache_write("{key}")'), 0, "default", value * 1000000)
        for key, value in pairs
        if value is not None
    )


def _token_policy(metadata: Mapping[str, object]) -> dict[str, JsonValue] | None:
    if unsupported_catalog_pricing_metadata(metadata):
        return None
    numeric: Final = tuple((key, value) for key, value in metadata.items() if "cost" in key and value is not None)
    parsed: Final = tuple(_parse_rate(key, value) for key, value in numeric)
    if not parsed or any(rate is None for rate in parsed):
        return None
    explicit: Final = _explicit_cache_rates(metadata)
    if explicit is None:
        return None
    replaced: Final = frozenset(rate.variable for rate in explicit)
    rates: Final = tuple(rate for rate in parsed if rate is not None and rate.variable not in replaced) + explicit
    if not any(rate.variable == "p" and rate.threshold == 0 and rate.service == "default" for rate in rates):
        return None
    if metadata.get("mode") not in {"embedding", "image_generation", "image_edit"} and not any(
        rate.variable == "c" and rate.threshold == 0 and rate.service == "default" for rate in rates
    ):
        return None
    if metadata.get("mode") in {"image_generation", "image_edit"} and not any(
        rate.variable in {"c", "img_o"} and rate.threshold == 0 and rate.service == "default" for rate in rates
    ):
        return None
    variables: Final = tuple(dict.fromkeys(("p", "c", "cr", "cc", *(rate.variable for rate in rates))))
    services: Final = tuple(sorted({rate.service for rate in rates if rate.service != "default"}))
    branches: Final = tuple(
        f'param("service_tier", "default") == "{service}" ? {_contexts(rates, service, variables)} : '
        for service in services
    )
    expression: Final = "".join(branches) + _contexts(rates, "default", variables)
    try:
        return validate_expression_policy(
            {
                "version": "expression-v1",
                "expression": expression,
                "timezone": "UTC",
                "usage_bounds": {},
            }
        ).model_dump(mode="json")
    except ValueError:
        return None


def catalog_expression_policy(name: str, metadata: Mapping[str, object]) -> dict[str, JsonValue] | None:
    from .catalog_image_expression import catalog_image_expression_policy, catalog_search_expression_policy

    if unsupported_catalog_pricing_metadata(metadata):
        return None
    if metadata.get("mode") not in {None, "chat", "completion", "embedding", "image_generation", "image_edit"}:
        return None
    if metadata.get("mode") in {"image_generation", "image_edit"}:
        return catalog_image_expression_policy(name, metadata, _token_policy)
    token_metadata: Final = {
        key: value
        for key, value in metadata.items()
        if key not in {"search_context_cost_per_query", "google_maps_grounding_cost_per_query"}
    }
    policy: Final = _token_policy(token_metadata)
    return catalog_search_expression_policy(policy, metadata) if policy is not None else None
