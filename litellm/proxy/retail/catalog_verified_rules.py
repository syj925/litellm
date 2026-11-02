from collections.abc import Mapping
from decimal import Context, Decimal, InvalidOperation, localcontext
from itertools import product
from typing import Final

from pydantic import JsonValue

from .expression_pricing import validate_expression_policy

_CONTEXT_FIELDS: Final = ("input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost")
_SERVICES: Final = ("batches", "flex")
# https://ai.google.dev/gemini-api/docs/pricing, verified 2026-11-02; USD per token
_ARGON_INTRODUCTORY: Final = (Decimal("0.000002"), Decimal("0.000010"), Decimal("0.0000001"))
_ARGON_STANDARD: Final = tuple(amount * 2 for amount in _ARGON_INTRODUCTORY)


def _google_model(name: str, metadata: Mapping[str, object], leaf: str) -> bool:
    provider: Final = metadata.get("litellm_provider")
    return name.rpartition("/")[2] == leaf and (
        provider == "gemini" or isinstance(provider, str) and provider.startswith("vertex_ai")
    )


def _rate(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if isinstance(value, int) and value.bit_length() > 128:
        return None
    text: Final = str(value)
    if len(text) > 128:
        return None
    try:
        amount: Final = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite() or not 0 <= amount <= 1_000_000 or (amount != 0 and amount.adjusted() < -36):
        return None
    return amount


def _missing_long_rate(metadata: Mapping[str, object], field: str, service: str) -> tuple[str, str] | None:
    target: Final = f"{field}_above_200k_tokens_{service}"
    if target in metadata:
        return None
    base: Final = _rate(metadata.get(field))
    long: Final = _rate(metadata.get(f"{field}_above_200k_tokens"))
    short_service: Final = _rate(metadata.get(f"{field}_{service}"))
    if base is None or base == 0 or long is None or short_service is None:
        return None
    with localcontext(Context(prec=80)):
        amount: Final = long * (short_service / base)
    return (target, format(amount, "f")) if _rate(amount) is not None else None


def enrich_catalog_rates(name: str, metadata: Mapping[str, object]) -> Mapping[str, object]:
    if not _google_model(name, metadata, "gemini-2.5-pro"):
        return metadata
    candidates: Final = tuple(
        _missing_long_rate(metadata, field, service) for field, service in product(_CONTEXT_FIELDS, _SERVICES)
    )
    additions: Final = {pair[0]: pair[1] for pair in candidates if pair is not None}
    return {**metadata, **additions} if additions else metadata


def apply_catalog_calendar(
    name: str,
    metadata: Mapping[str, object],
    policy: Mapping[str, object],
) -> dict[str, JsonValue] | None:
    try:
        validated: Final = validate_expression_policy(policy)
        serialized: Final = validated.model_dump(mode="json")
        if not _google_model(name, metadata, "gemini-4-argon"):
            return serialized
        signature: Final = tuple(_rate(metadata.get(field)) for field in _CONTEXT_FIELDS)
        if signature == _ARGON_STANDARD:
            return serialized
        if signature != _ARGON_INTRODUCTORY:
            return None
        return validate_expression_policy(
            {
                **serialized,
                "expression": (
                    f'({validated.expression}) * (date("UTC") >= "2026-12-01" '
                    '? tier("Gemini 4 Argon: standard from 2026-12-01", 2) '
                    ': tier("Gemini 4 Argon: introductory through 2026-11-30", 1))'
                ),
            }
        ).model_dump(mode="json")
    except ValueError:
        return None
