from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Final, TypeAlias

from pydantic import JsonValue

from .expression_pricing import validate_expression_policy

TokenPolicyBuilder: TypeAlias = Callable[[Mapping[str, object]], dict[str, JsonValue] | None]
_ALTERNATIVE_FIELDS: Final = frozenset((
    "output_cost_per_image", "input_cost_per_image", "search_context_cost_per_query",
    "google_maps_grounding_cost_per_query",
))
_SEARCH_CONTEXT_FIELDS: Final = frozenset((
    "search_context_size_low", "search_context_size_medium", "search_context_size_high",
))


def _rate(value: object) -> Decimal | None:
    try:
        amount: Final = Decimal(str(value))
    except InvalidOperation:
        return None
    if not amount.is_finite() or not 0 <= amount <= 1_000_000:
        return None
    if amount != 0 and (amount.adjusted() < -36 or len(amount.as_tuple().digits) > 256):
        return None
    return Decimal(0) if amount == 0 else amount


def _grounding(metadata: Mapping[str, object]) -> tuple[str, dict[str, str]] | None:
    configured: Final = metadata.get("search_context_cost_per_query")
    if configured is None:
        return "", {}
    provider: Final = metadata.get("litellm_provider")
    supported: Final = provider in ("openai", "anthropic", "gemini") or (
        isinstance(provider, str) and provider.startswith("vertex_ai")
    )
    if not supported:
        return None
    if not isinstance(configured, Mapping):
        return None
    amount: Final = _rate(configured.get("search_context_size_medium"))
    if amount is None:
        return None
    requests: Final = 'u("web_search_requests")'
    if provider in ("openai", "anthropic"):
        if metadata.get("web_search_billing_unit") not in (None, "per_query"):
            return None
        if any(key not in _SEARCH_CONTEXT_FIELDS for key in configured):
            return None
        if provider == "openai" and (
            frozenset(configured) != _SEARCH_CONTEXT_FIELDS
            or any(_rate(value) != amount for value in configured.values())
        ):
            return None
        return f' + fixed({requests} * {format(amount, "f")})', {"web_search_requests": "64"}
    unit: Final = metadata.get("web_search_billing_unit", "per_prompt") or "per_prompt"
    if unit not in ("per_query", "per_prompt"):
        return None
    quantity: Final = requests if unit == "per_query" else f"min(1, {requests})"
    return f' + fixed({quantity} * {format(amount, "f")})', {"web_search_requests": "64"}


def _maps_grounding(metadata: Mapping[str, object]) -> tuple[str, dict[str, str]] | None:
    configured: Final = metadata.get("google_maps_grounding_cost_per_query")
    if configured is None:
        return "", {}
    provider: Final = metadata.get("litellm_provider")
    if provider != "gemini" and not (isinstance(provider, str) and provider.startswith("vertex_ai")):
        return None
    amount: Final = _rate(configured)
    unit: Final = metadata.get("web_search_billing_unit", "per_prompt") or "per_prompt"
    if amount is None or unit not in ("per_query", "per_prompt"):
        return None
    requests: Final = 'u("google_maps_grounding_requests")'
    quantity: Final = requests if unit == "per_query" else f"min(1, {requests})"
    return f' + fixed({quantity} * {format(amount, "f")})', {"google_maps_grounding_requests": "64"}


def catalog_search_expression_policy(
    policy: Mapping[str, object], metadata: Mapping[str, object],
) -> dict[str, JsonValue] | None:
    grounding: Final = _grounding(metadata)
    maps: Final = _maps_grounding(metadata)
    if grounding is None or maps is None:
        return None
    try:
        validated: Final = validate_expression_policy(policy)
        return validate_expression_policy({
            "version": "expression-v1",
            "expression": f"({validated.expression}){grounding[0]}{maps[0]}",
            "timezone": validated.timezone,
            "usage_bounds": {**dict(validated.usage_bounds), **grounding[1], **maps[1]},
        }).model_dump(mode="json")
    except ValueError:
        return None


def catalog_image_expression_policy(
    name: str,
    metadata: Mapping[str, object],
    token_policy_builder: TokenPolicyBuilder,
) -> dict[str, JsonValue] | None:
    from .catalog_expression import unsupported_catalog_pricing_metadata

    if metadata.get("mode") not in {"image_generation", "image_edit"}:
        return None
    if unsupported_catalog_pricing_metadata(metadata):
        return None
    raw_image: Final = metadata.get("output_cost_per_image")
    image_rate: Final = _rate(raw_image) if raw_image is not None else None
    raw_input_image: Final = metadata.get("input_cost_per_image")
    if (raw_image is not None and image_rate is None) or (
        raw_input_image is not None and _rate(raw_input_image) is None
    ):
        return None
    if _grounding(metadata) is None or _maps_grounding(metadata) is None:
        return None
    token_metadata: Final = {key: value for key, value in metadata.items() if key not in _ALTERNATIVE_FIELDS}
    token_policy: Final = token_policy_builder(token_metadata)
    token_dimensions: Final = any("cost" in key and value is not None for key, value in token_metadata.items())
    if token_policy is None and (token_dimensions or image_rate is None):
        return None
    try:
        validated_token: Final = validate_expression_policy(token_policy) if token_policy is not None else None
        flat_expression: Final = f'fixed(u("images") * {format(image_rate, "f")})' if image_rate is not None else None
        token_expression: Final = validated_token.expression if validated_token is not None else None
        expression: Final = (
            f'(reported("tokens") ? tier("image token usage", ({token_expression})) : '
            f'tier("returned images", {flat_expression}))'
            if token_expression is not None and flat_expression is not None
            else token_expression if token_expression is not None else flat_expression
        )
        base_policy: Final = validate_expression_policy({
            "version": "expression-v1",
            "expression": expression,
            "timezone": validated_token.timezone if validated_token is not None else "UTC",
            "usage_bounds": {
                **(dict(validated_token.usage_bounds) if validated_token is not None else {}),
                **({"images": "16"} if flat_expression is not None else {}),
            },
        }).model_dump(mode="json")
        return catalog_search_expression_policy(base_policy, metadata)
    except ValueError:
        return None
