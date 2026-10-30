from collections.abc import Mapping, Sequence
from datetime import datetime
from math import isfinite
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue

SourceName = Literal["models_dev", "openrouter"]


class BenchmarkRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    score: float
    metric: str | None = None
    source_url: str
    source: SourceName
    retrieved_at: datetime
    version: str | None = None
    harness: str | None = None
    dataset: str | None = None
    variant: str | None = None
    evaluated_at: str | None = None
    rank: int | None = None


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
    benchmarks: tuple[BenchmarkRecord, ...] = ()
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


def _score(value: JsonValue) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number: Final = float(value)
        return number if isfinite(number) else None
    except OverflowError:
        return None


def _citation(value: JsonValue) -> str | None:
    text: Final = _text(value, 2049)
    if text is None or len(text) > 2048 or any(ord(char) < 33 for char in text):
        return None
    try:
        parsed: Final = urlsplit(text)
        return text if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


def _benchmark(raw: JsonValue, retrieved_at: datetime) -> BenchmarkRecord | None:
    item: Final = mapping(raw)
    name: Final = _text(item.get("name"), 300)
    score: Final = _score(item.get("score"))
    citation: Final = _citation(item.get("source"))
    if name is None or score is None or citation is None:
        return None
    return BenchmarkRecord(
        name=name, score=score, metric=_text(item.get("metric"), 100), source_url=citation,
        source="models_dev", retrieved_at=retrieved_at,
        version=_text(item.get("version"), 100), harness=_text(item.get("harness"), 300),
        dataset=_text(item.get("dataset"), 300), variant=_text(item.get("variant"), 300),
        evaluated_at=_text(item.get("date"), 100),
    )


def _model_benchmarks(model: Mapping[str, JsonValue], retrieved_at: datetime) -> tuple[BenchmarkRecord, ...]:
    candidates: Final = tuple(_benchmark(raw, retrieved_at) for raw in _sequence(model.get("benchmarks")))
    return tuple(item for item in candidates if item is not None)


def _arena_benchmark(raw: JsonValue, citation: str, retrieved_at: datetime) -> BenchmarkRecord | None:
    item: Final = mapping(raw)
    score: Final = _score(item.get("elo"))
    category: Final = _text(item.get("category"), 300)
    arena: Final = _text(item.get("arena"), 100)
    if score is None or category is None or arena is None:
        return None
    return BenchmarkRecord(
        name="Design Arena", score=score, metric="Elo", source_url=citation,
        source="openrouter", retrieved_at=retrieved_at, dataset=arena, variant=category,
        rank=_integer(item.get("rank")),
    )


def _openrouter_benchmarks(
    model: Mapping[str, JsonValue], identifier: str, retrieved_at: datetime
) -> tuple[BenchmarkRecord, ...]:
    benchmarks: Final = mapping(model.get("benchmarks"))
    analysis: Final = mapping(benchmarks.get("artificial_analysis"))
    citation: Final = "https://openrouter.ai/" + identifier + "/benchmarks"
    names: Final = {
        "intelligence_index": "Artificial Analysis Intelligence Index",
        "coding_index": "Artificial Analysis Coding Index",
        "agentic_index": "Artificial Analysis Agentic Index",
    }
    indices: Final = tuple(
        BenchmarkRecord(
            name=name, score=score, metric="index", source="openrouter", source_url=citation,
            retrieved_at=retrieved_at,
        )
        for key, name in names.items() if (score := _score(analysis.get(key))) is not None
    )
    arena: Final = tuple(
        _arena_benchmark(raw, citation, retrieved_at) for raw in _sequence(benchmarks.get("design_arena"))
    )
    return (*indices, *(item for item in arena if item is not None))


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
        benchmarks=_model_benchmarks(model, retrieved_at),
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
        benchmarks=_openrouter_benchmarks(model, identifier, retrieved_at),
        source="openrouter",
        source_url="https://openrouter.ai/api/v1/models",
        retrieved_at=retrieved_at,
    )


def normalize_openrouter(
    document: Mapping[str, JsonValue], retrieved_at: datetime
) -> tuple[ExternalMetadataRecord, ...]:
    candidates: Final = tuple(_openrouter_record(raw, retrieved_at) for raw in _sequence(document.get("data")))
    return tuple(record for record in candidates if record is not None)
