import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Final

_OPENAI_REGIONS: Final = (
    ("regional_processing_uplift_multiplier_eu", "eu"),
    ("regional_processing_uplift_multiplier_us", "us"),
)
_ALLOWANCE_KEYS: Final = frozenset(("count", "period", "shared_group"))


def _multiplier(value: object) -> Decimal | None:
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
    return amount if amount.is_finite() and 1 <= amount <= 100 else None


def _provider_entry(value: object, provider: object) -> bool:
    if not isinstance(value, Mapping) or any(key not in {"us", "fast", "bedrock_invocation_schema"} for key in value):
        return False
    modifiers: Final = tuple(key for key in value if key != "bedrock_invocation_schema")
    return (not modifiers or provider == "anthropic") and all(_multiplier(value[key]) is not None for key in modifiers)


def _google_provider(provider: object) -> bool:
    return provider == "gemini" or isinstance(provider, str) and provider.startswith("vertex_ai")


def _allowance(value: object, provider: object) -> bool:
    if not _google_provider(provider) or not isinstance(value, Mapping) or frozenset(value) != _ALLOWANCE_KEYS:
        return False
    count: Final = value.get("count")
    group: Final = value.get("shared_group")
    return (
        isinstance(count, int)
        and not isinstance(count, bool)
        and 0 < count <= 1_000_000_000_000
        and value.get("period") in ("day", "month")
        and isinstance(group, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", group) is not None
    )


def supported_pricing_metadata(metadata: Mapping[str, object]) -> frozenset[str]:
    provider: Final = metadata.get("litellm_provider")
    provider_entry: Final = metadata.get("provider_specific_entry")
    vertex: Final = metadata.get("regional_endpoint_uplift_multiplier")
    supported: Final = (
        *(("provider_specific_entry",) if _provider_entry(provider_entry, provider) else ()),
        *(key for key, _ in _OPENAI_REGIONS if provider == "openai" and _multiplier(metadata.get(key)) is not None),
        *(
            ("regional_endpoint_uplift_multiplier",)
            if isinstance(provider, str) and provider.startswith("vertex_ai") and _multiplier(vertex) is not None
            else ()
        ),
        *(("search_free_requests",) if _allowance(metadata.get("search_free_requests"), provider) else ()),
    )
    return frozenset(supported)


def _factor(attribute: str, value: str, multiplier: object, label: str, *, different: bool = False) -> str:
    amount: Final = _multiplier(multiplier)
    if amount is None:
        return "1"
    operator: Final = "!=" if different else "=="
    return f'(attr("{attribute}") {operator} "{value}" ? tier("{label}", {format(amount, "f")}) : 1)'


def apply_token_modifiers(expression: str, metadata: Mapping[str, object]) -> str:
    supported: Final = supported_pricing_metadata(metadata)
    entry: Final = metadata.get("provider_specific_entry")
    anthropic: Final = entry if "provider_specific_entry" in supported and isinstance(entry, Mapping) else {}
    factors: Final = (
        *(_factor("inference_geo", "us", anthropic["us"], "inference geo: us") for _ in (0,) if "us" in anthropic),
        *(_factor("speed", "fast", anthropic["fast"], "speed: fast") for _ in (0,) if "fast" in anthropic),
        *(
            _factor("data_residency", region, metadata[key], f"data residency: {region}")
            for key, region in _OPENAI_REGIONS
            if key in supported
        ),
        *(
            (
                _factor(
                    "vertex_location",
                    "global",
                    metadata["regional_endpoint_uplift_multiplier"],
                    "Vertex regional endpoint",
                    different=True,
                ),
            )
            if "regional_endpoint_uplift_multiplier" in supported
            else ()
        ),
    )
    return f"({expression}) * " + " * ".join(factors) if factors else expression


def catalog_pricing_note(metadata: Mapping[str, object]) -> str | None:
    if "search_free_requests" not in supported_pricing_metadata(metadata):
        return None
    return (
        "Provider shared free allowances are not passed through. "
        "Reported grounding usage is charged at the listed paid rates"
    )
