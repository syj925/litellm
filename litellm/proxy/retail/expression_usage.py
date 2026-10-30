from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import JsonValue

from .expression_parser import CACHE_TTL_VARIABLES
from .expression_pricing import (
    ExpressionPolicy,
    ExpressionPricingError,
    expression_cache_ttls,
    expression_reported_usage_keys,
    expression_usage_keys,
    expression_variables,
)
from .pricing import UsageFacts, as_mapping, response_usage, usage_facts

_INPUT_DETAILS: Final = ("prompt_tokens_details", "input_tokens_details", "input_details")
_OUTPUT_DETAILS: Final = ("completion_tokens_details", "output_tokens_details", "output_details")
_IMAGE_TYPES: Final = frozenset(("image_url", "input_image", "image", "document", "file", "input_file"))
_AUDIO_TYPES: Final = frozenset(("input_audio", "audio_url", "audio"))


def _details(usage: Mapping[str, object], keys: Sequence[str]) -> tuple[Mapping[str, object], ...]:
    return tuple(as_mapping(usage.get(key)) for key in keys)


def _count(sources: Sequence[Mapping[str, object]], key: str) -> int | None:
    values: Final = tuple(source[key] for source in sources if source.get(key) is not None)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        raise ValueError(f"Invalid reported {key}")
    integers: Final = tuple(value for value in values if isinstance(value, int))
    return max(integers) if integers else None


def _modality_list(usage: Mapping[str, object], field: str, modality: str) -> int | None:
    entries: Final = usage.get(field)
    if not isinstance(entries, (tuple, list)):
        return None
    matches: Final = tuple(as_mapping(item) for item in entries if as_mapping(item).get("modality") == modality)
    return _count(matches, "tokenCount") if matches else 0


def _modality_count(
    usage: Mapping[str, object],
    details: Sequence[Mapping[str, object]],
    field: str,
    list_field: str,
    modality: str,
) -> int | None:
    direct: Final = _count(details, field)
    return direct if direct is not None else _modality_list(usage, list_field, modality)


def _numeric(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ExpressionPricingError("A billing quantity must be a reported finite non-negative number")
    if isinstance(value, str) and len(value) > 256:
        raise ValueError("A billing quantity exceeds 256 characters")
    if isinstance(value, int) and value.bit_length() > 128:
        raise ValueError("A billing quantity is too large")
    try:
        number: Final = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("A billing quantity must be numeric") from exc
    if not number.is_finite() or not 0 <= number <= 1_000_000_000_000:
        raise ValueError("A billing quantity is outside its supported range")
    if number and (number.adjusted() < -36 or len(number.as_tuple().digits) > 256):
        raise ValueError("A billing quantity exceeds its supported decimal precision")
    return "0" if number == 0 else format(number, "f")


def _path(value: Mapping[str, object], path: str) -> object:
    first, separator, remaining = path.partition(".")
    found: Final = value.get(first)
    return _path(as_mapping(found), remaining) if separator else found


def _returned_images(response: Mapping[str, object]) -> int | None:
    data: Final = response.get("data")
    if not isinstance(data, (tuple, list)):
        return None
    images: Final = tuple(
        item
        for item in data
        if any(isinstance(as_mapping(item).get(key), str) and as_mapping(item).get(key) for key in ("url", "b64_json"))
    )
    return len(images) if images or not data else None


def _reported_units(
    response: Mapping[str, object],
    usage: Mapping[str, object],
    evidence: Mapping[str, object],
) -> Mapping[str, JsonValue]:
    images: Final = _returned_images(response)
    counts: Final = tuple(_numeric(usage[key]) for key in ("images", "image_count") if usage.get(key) is not None)
    if counts and any(Decimal(value) != Decimal(counts[0]) for value in counts):
        raise ValueError("Reported image counts disagree")
    if images is not None and counts and any(Decimal(value) != images for value in counts):
        raise ValueError("Reported image count does not match returned images")
    actual: Final = images if images is not None and (images > 0 or evidence.get("output_image")) else None
    reported: Final = counts[0] if counts else None
    quantity: Final = str(actual) if actual is not None else reported
    return {
        "requests": "1",
        **({"images": quantity, "image_count": quantity} if quantity is not None else {}),
        **{
            key: _numeric(usage[key])
            for key in ("audio_seconds", "video_seconds", "duration", "characters")
            if usage.get(key) is not None
        },
        **_reported_search_units(usage, evidence),
    }


def _search_quantity(usage: Mapping[str, object], key: str, expected: object) -> str | None:
    sources: Final = (usage, as_mapping(usage.get("server_tool_use")), *_details(usage, _INPUT_DETAILS))
    reported: Final = tuple(_numeric(source[key]) for source in sources if source.get(key) is not None)
    if reported:
        return format(max(Decimal(value) for value in reported), "f")
    return None if expected else "0"


def _reported_search_units(usage: Mapping[str, object], evidence: Mapping[str, object]) -> Mapping[str, JsonValue]:
    web: Final = _search_quantity(usage, "web_search_requests", evidence.get("web_search"))
    maps: Final = _search_quantity(usage, "google_maps_grounding_requests", evidence.get("google_maps"))
    return {
        **({"web_search_requests": web} if web is not None else {}),
        **({"google_maps_grounding_requests": maps} if maps is not None else {}),
    }


def _has_input_modality(value: object, modality: str, depth: int = 0) -> bool:
    if depth > 32:
        raise ValueError("Request modality structure exceeds 32 levels")
    types: Final = _IMAGE_TYPES if modality == "image" else _AUDIO_TYPES
    if isinstance(value, Mapping):
        kind: Final = value.get("type")
        if isinstance(kind, str) and kind in types:
            return True
        markers: Final = (
            ("image_url", "input_image", "file_id") if modality == "image" else ("input_audio", "audio_url")
        )
        return any(value.get(key) is not None for key in markers) or any(
            _has_input_modality(child, modality, depth + 1) for child in value.values()
        )
    if isinstance(value, (tuple, list)):
        return any(_has_input_modality(child, modality, depth + 1) for child in value)
    return False


def request_usage_evidence(data: Mapping[str, object], call_type: str) -> Mapping[str, JsonValue]:
    inputs: Final = tuple(data.get(key) for key in ("messages", "input", "file", "attachments", "image"))
    modalities: Final = data.get("modalities")
    requested: Final = tuple(modalities) if isinstance(modalities, (tuple, list)) else ()
    reasoning: Final = data.get("reasoning_effort", _path(data, "reasoning.effort"))
    return {
        "input_image": any(_has_input_modality(value, "image") for value in inputs)
        or call_type in {"image_edit", "aimage_edit"},
        "input_audio": any(_has_input_modality(value, "audio") for value in inputs)
        or call_type in {"transcription", "atranscription"},
        "output_image": call_type in {"image_generation", "aimage_generation", "image_edit", "aimage_edit"}
        or "image" in requested,
        "image_response": call_type in {"image_generation", "aimage_generation", "image_edit", "aimage_edit"},
        "output_audio": call_type in {"speech", "aspeech"} or "audio" in requested,
        "reasoning": reasoning is not None and reasoning != "none",
        "web_search": _has_search_tool(data.get("tools")),
        "google_maps": _has_search_tool(data.get("tools"), maps=True),
    }


def _has_search_tool(value: object, *, maps: bool = False, depth: int = 0) -> bool:
    if depth > 32:
        raise ValueError("Request tool structure exceeds 32 levels")
    if isinstance(value, (tuple, list)):
        return any(_has_search_tool(tool, maps=maps, depth=depth + 1) for tool in value)
    if not isinstance(value, Mapping):
        return False
    keys: Final = ("googleMaps", "google_maps") if maps else ("googleSearch", "google_search")
    kind: Final = value.get("type")
    prefix: Final = "google_maps" if maps else "web_search"
    return any(key in value and value[key] is not None for key in keys) or (
        isinstance(kind, str) and (kind == prefix or kind.startswith(prefix + "_"))
    )


def _modality_total(reported: int | None, expected: object) -> int | None:
    return reported if reported is not None else None if expected else 0


def _cached_modality(total: int | None, cached: int | None, cache_read: int | None) -> int | None:
    if total is not None and cached is not None and cached > total:
        raise ValueError("Cached modality tokens exceed the modality total")
    if cached is not None and cache_read is not None and cached > cache_read:
        raise ValueError("Cached modality tokens exceed the reported cache total")
    if cached is not None:
        return cached
    return 0 if total == 0 or cache_read == 0 else None


def _difference(total: int | None, subset: int | None) -> int | None:
    return total - subset if total is not None and subset is not None else None


def _input_modalities(
    usage: Mapping[str, object],
    evidence: Mapping[str, object],
    cache_read: int | None,
) -> Mapping[str, int | None]:
    details: Final = _details(usage, _INPUT_DETAILS)
    cached: Final = tuple(as_mapping(item.get("cached_tokens_details")) for item in details)
    reported_cached_image: Final = _modality_count(usage, cached, "image_tokens", "cacheTokensDetails", "IMAGE")
    reported_cached_audio: Final = _modality_count(usage, cached, "audio_tokens", "cacheTokensDetails", "AUDIO")
    image: Final = _modality_total(
        _modality_count(usage, details, "image_tokens", "promptTokensDetails", "IMAGE"),
        evidence.get("input_image") or reported_cached_image,
    )
    audio: Final = _modality_total(
        _modality_count(usage, details, "audio_tokens", "promptTokensDetails", "AUDIO"),
        evidence.get("input_audio") or reported_cached_audio,
    )
    cached_image: Final = _cached_modality(image, reported_cached_image, cache_read)
    cached_audio: Final = _cached_modality(audio, reported_cached_audio, cache_read)
    return {"img": _difference(image, cached_image), "ai": _difference(audio, cached_audio), "img_cr": cached_image}


def _output_modalities(
    usage: Mapping[str, object],
    evidence: Mapping[str, object],
    standard: UsageFacts | None,
) -> Mapping[str, int | None]:
    details: Final = _details(usage, _OUTPUT_DETAILS)
    reported_image: Final = _modality_count(usage, details, "image_tokens", "candidatesTokensDetails", "IMAGE")
    image: Final = (
        standard.output_tokens
        if (reported_image is None and standard is not None and evidence.get("image_response"))
        else reported_image
    )
    reasoning: Final = _count(details, "reasoning_tokens")
    thoughts: Final = _count((usage,), "thoughtsTokenCount")
    reported_reasoning: Final = reasoning if reasoning is not None else thoughts
    return {
        "img_o": _modality_total(
            image,
            evidence.get("output_image"),
        ),
        "ao": _modality_total(
            _modality_count(usage, details, "audio_tokens", "candidatesTokensDetails", "AUDIO"),
            evidence.get("output_audio"),
        ),
        "reasoning": _modality_total(reported_reasoning, evidence.get("reasoning")),
    }


def _subtract_selected(total: int, quantities: Mapping[str, int | None], used: frozenset[str]) -> int | None:
    selected: Final = tuple(value for key, value in quantities.items() if key in used)
    if any(value is None for value in selected):
        return None
    count: Final = sum(value for value in selected if value is not None)
    if count > total:
        raise ValueError("Separately priced usage exceeds the reported token total")
    return total - count


def _standard_variables(
    standard: UsageFacts,
    inputs: Mapping[str, int | None],
    outputs: Mapping[str, int | None],
    used: frozenset[str],
    cache_ttls: Mapping[str, int],
) -> Mapping[str, int | None]:
    tiers: Final = {
        variable: standard.cache_write_ttl_tokens.get(ttl, 0) for variable, ttl in CACHE_TTL_VARIABLES.items()
    }
    return {
        "p": _subtract_selected(standard.input_tokens, {"img": inputs["img"], "ai": inputs["ai"]}, used),
        "c": _subtract_selected(standard.output_tokens, outputs, used),
        "len": standard.input_tokens + standard.cache_read_tokens + standard.cache_write_tokens,
        "cr": _subtract_selected(standard.cache_read_tokens, {"img_cr": inputs["img_cr"]}, used),
        "cc": standard.cache_write_tokens - sum(cache_ttls.values()),
        **tiers,
    }


def _known_variables(variables: Mapping[str, int | None]) -> Mapping[str, JsonValue]:
    return {key: value for key, value in variables.items() if value is not None}


def _unit_value(key: str, units: Mapping[str, JsonValue], usage: Mapping[str, object]) -> str | None:
    value: Final = units.get(key, _path(usage, key))
    return _numeric(value) if value is not None else None


def expression_usage_facts(
    response: object,
    policy: ExpressionPolicy,
    evidence: Mapping[str, object],
    *,
    allow_input_only: bool = False,
) -> Mapping[str, JsonValue]:
    payload: Final = as_mapping(response)
    usage: Final = response_usage(response)
    used: Final = expression_variables(policy)
    standard: Final = usage_facts(response, allow_input_only=allow_input_only)
    inputs: Final = _input_modalities(usage, evidence, standard.cache_read_tokens if standard else None)
    outputs: Final = _output_modalities(usage, evidence, standard)
    cache_ttls: Final = (
        {ttl: standard.cache_write_ttl_tokens.get(ttl, 0) for ttl in expression_cache_ttls(policy)} if standard else {}
    )
    variables: Final = _standard_variables(standard, inputs, outputs, used, cache_ttls) if standard else {}
    units: Final = _reported_units(payload, usage, evidence)
    unit_keys: Final = expression_usage_keys(policy).union(expression_reported_usage_keys(policy))
    unit_pairs: Final = tuple((key, _unit_value(key, units, usage)) for key in unit_keys)
    total: Final = (
        (standard.input_tokens + standard.cache_read_tokens + standard.cache_write_tokens + standard.output_tokens)
        if standard
        else 0
    )
    return {
        **({"reported_tokens": standard.model_dump()} if standard else {}),
        **_known_variables(variables),
        **_known_variables(inputs),
        **_known_variables(outputs),
        "usage": {key: value for key, value in unit_pairs if value is not None},
        "cache_write_ttl_tokens": cache_ttls,
        **({"tokens": total} if total > 0 else {}),
    }
