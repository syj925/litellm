from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

SourceName = Literal["models_dev", "openrouter"]


class ExternalMetadataRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=500)
    aliases: tuple[str, ...] = ()
    provider: str | None = None
    name: str | None = None
    description: str | None = None
    context_length: int | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    capabilities: dict[str, bool | None] = Field(default_factory=dict)
    source: SourceName
    source_url: str
    retrieved_at: datetime


def mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _sequence(value: JsonValue) -> Sequence[JsonValue]:
    return value if isinstance(value, list) else ()


def _text(value: JsonValue, limit: int = 20000) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _integer(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _boolean(value: JsonValue) -> bool | None:
    return value if isinstance(value, bool) else None


def _modality(value: str) -> str:
    if value.startswith("image"):
        return "image"
    if value.startswith("audio"):
        return "audio"
    if value.startswith("video"):
        return "video"
    if value in {"pdf", "application/pdf"}:
        return "pdf"
    return value


def _modalities(value: JsonValue) -> tuple[str, ...]:
    return tuple(sorted({_modality(item) for item in _sequence(value) if isinstance(item, str)}))


def _modality_support(modalities: tuple[str, ...], value: str) -> bool | None:
    if value == "pdf" and "file" in modalities and "pdf" not in modalities:
        return None
    return value in modalities if modalities else None


def _model_capabilities(
    model: Mapping[str, JsonValue],
    service: Mapping[str, JsonValue],
    inputs: tuple[str, ...],
    outputs: tuple[str, ...],
) -> dict[str, bool | None]:
    capabilities: Final = mapping(model.get("capabilities"))
    tools: Final = mapping(capabilities.get("tools"))
    reasoning: Final = mapping(capabilities.get("reasoning"))
    cache: Final = mapping(service.get("cache"))
    return {
        "tools": _boolean(model.get("tool_call", tools.get("supported"))),
        "reasoning": _boolean(model.get("reasoning", reasoning.get("supported"))),
        "vision": _modality_support(inputs, "image"),
        "audio_input": _modality_support(inputs, "audio"),
        "audio_output": _modality_support(outputs, "audio"),
        "pdf_input": _modality_support(inputs, "pdf"),
        "structured_output": _boolean(service.get("structured_output")),
        "prompt_caching": _boolean(cache.get("supported", service.get("prompt_caching"))),
    }


def _first_party_service(identifier: str, providers: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    lab, _, model = identifier.partition("/")
    provider: Final = mapping(providers.get(lab))
    models: Final = mapping(provider.get("models"))
    service: Final = mapping(models.get(model))
    canonical: Final = service.get("canonical_model_id")
    return service if canonical in (None, identifier) else {}


def _models_dev_record(
    identifier: str,
    raw: JsonValue,
    providers: Mapping[str, JsonValue],
    retrieved_at: datetime,
) -> ExternalMetadataRecord | None:
    if not identifier or "/" not in identifier or len(identifier) > 500:
        return None
    model: Final = mapping(raw)
    if not model or model.get("id", identifier) != identifier:
        return None
    service: Final = _first_party_service(identifier, providers)
    modalities: Final = mapping(model.get("modalities"))
    limit: Final = mapping(model.get("limit"))
    inputs: Final = _modalities(modalities.get("input"))
    outputs: Final = _modalities(modalities.get("output"))
    return ExternalMetadataRecord(
        id=identifier,
        aliases=tuple(
            alias
            for alias, entry in mapping(mapping(providers.get("openrouter")).get("models")).items()
            if mapping(entry).get("canonical_model_id") == identifier and ":" not in alias
        ),
        provider=identifier.partition("/")[0],
        name=_text(model.get("name"), 500),
        description=_text(model.get("description")),
        context_length=_integer(limit.get("input")),
        context_window=_integer(limit.get("context")),
        max_output_tokens=_integer(limit.get("output")),
        input_modalities=inputs,
        output_modalities=outputs,
        capabilities=_model_capabilities(model, service, inputs, outputs),
        source="models_dev",
        source_url="https://models.dev/models.json",
        retrieved_at=retrieved_at,
    )


def normalize_models_dev(
    models: Mapping[str, JsonValue],
    providers: Mapping[str, JsonValue],
    retrieved_at: datetime,
) -> tuple[ExternalMetadataRecord, ...]:
    candidates: Final = tuple(
        _models_dev_record(identifier, raw, providers, retrieved_at) for identifier, raw in models.items()
    )
    return tuple(record for record in candidates if record is not None)


def _openrouter_record(raw: JsonValue, retrieved_at: datetime) -> ExternalMetadataRecord | None:
    model: Final = mapping(raw)
    identifier: Final = _text(model.get("id"), 501)
    if identifier is None or "/" not in identifier or len(identifier) > 500:
        return None
    architecture: Final = mapping(model.get("architecture"))
    top_provider: Final = mapping(model.get("top_provider"))
    inputs: Final = _modalities(architecture.get("input_modalities"))
    outputs: Final = _modalities(architecture.get("output_modalities"))
    parameters: Final = frozenset(
        item for item in _sequence(model.get("supported_parameters")) if isinstance(item, str)
    )
    return ExternalMetadataRecord(
        id=identifier,
        provider=identifier.partition("/")[0],
        name=_text(model.get("name"), 500),
        description=_text(model.get("description")),
        context_window=_integer(model.get("context_length")),
        max_output_tokens=_integer(top_provider.get("max_completion_tokens")),
        input_modalities=inputs,
        output_modalities=outputs,
        capabilities={
            "tools": True if "tools" in parameters else None,
            "reasoning": True if parameters.intersection({"reasoning", "reasoning_effort"}) else None,
            "structured_output": True if "structured_outputs" in parameters else None,
            "prompt_caching": True if parameters.intersection({"prompt_cache_key", "cache_control"}) else None,
            "vision": _modality_support(inputs, "image"),
            "audio_input": _modality_support(inputs, "audio"),
            "audio_output": _modality_support(outputs, "audio"),
            "pdf_input": _modality_support(inputs, "pdf"),
        },
        source="openrouter",
        source_url="https://openrouter.ai/api/v1/models",
        retrieved_at=retrieved_at,
    )


def normalize_openrouter(
    document: Mapping[str, JsonValue], retrieved_at: datetime
) -> tuple[ExternalMetadataRecord, ...]:
    candidates: Final = tuple(_openrouter_record(raw, retrieved_at) for raw in _sequence(document.get("data")))
    return tuple(record for record in candidates if record is not None)
