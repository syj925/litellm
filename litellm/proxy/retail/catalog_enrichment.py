from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from .catalog_sources import ExternalMetadataRecord
from .pricing import as_mapping


class MetadataSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: Literal["models_dev", "openrouter", "deployment", "litellm"]
    url: str | None = None
    retrieved_at: datetime | None = None
    fields: tuple[str, ...]
    model_id: str | None = None


class MetadataResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    values: dict[str, JsonValue]
    name: str | None = None
    context_window: int | None = None
    sources: tuple[MetadataSource, ...] = ()
    conflicts: tuple[str, ...] = ()


_OVERRIDES: Final = {
    "context_length": "max_input_tokens",
    "max_output_tokens": "max_output_tokens",
    "mode": "mode",
    "input_modalities": "supported_modalities",
    "output_modalities": "supported_output_modalities",
    "capabilities.vision": "supports_vision",
    "capabilities.tools": "supports_function_calling",
    "capabilities.reasoning": "supports_reasoning",
    "capabilities.prompt_caching": "supports_prompt_caching",
    "capabilities.structured_output": "supports_response_schema",
    "capabilities.audio_input": "supports_audio_input",
    "capabilities.audio_output": "supports_audio_output",
    "capabilities.pdf_input": "supports_pdf_input",
}
_SCALARS: Final = ("provider", "description", "context_length", "max_output_tokens", "input_modalities", "output_modalities")


def _author(name: str) -> str | None:
    lowered: Final = name.lower()
    if lowered.startswith("claude-"):
        return "anthropic"
    if lowered.startswith("gemini-"):
        return "google"
    if lowered.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4")):
        return "openai"
    return None


def _canonical(model: str, deployment: Mapping[str, object]) -> str | None:
    info: Final = as_mapping(deployment.get("model_info"))
    params: Final = as_mapping(deployment.get("litellm_params"))
    name: Final = info.get("base_model") or params.get("model") or model
    if not isinstance(name, str):
        return None
    head, separator, leaf = name.partition("/")
    if not separator:
        bare_author: Final = _author(name)
        return f"{bare_author}/{name}" if bare_author else None
    if head in {"openai", "anthropic", "google"}:
        return name
    if head in {"gemini", "vertex_ai", "azure"}:
        hosted_author: Final = _author(leaf)
        return f"{hosted_author}/{leaf}" if hosted_author else None
    return None


def _record_value(record: ExternalMetadataRecord | None, field: str) -> JsonValue:
    if record is None:
        return None
    if field.startswith("capabilities."):
        return record.capabilities.get(field.partition(".")[2])
    value: Final = getattr(record, field, None)
    if isinstance(value, tuple):
        return list(value) if value else None
    return value if isinstance(value, (str, int)) else None


def _baseline_value(baseline: Mapping[str, JsonValue], field: str) -> JsonValue:
    if field.startswith("capabilities."):
        capabilities: Final = baseline.get("capabilities")
        return capabilities.get(field.partition(".")[2]) if isinstance(capabilities, dict) else None
    value: Final = baseline.get(field)
    return value if value != [] else None


def _overridden(field: str, deployments: Sequence[Mapping[str, object]]) -> bool:
    key: Final = _OVERRIDES.get(field)
    return key is not None and any(as_mapping(item.get("model_info")).get(key) is not None for item in deployments)


class _FieldChoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    value: JsonValue
    source: Literal["models_dev", "openrouter", "deployment", "litellm"]
    record: ExternalMetadataRecord | None = None


def _choose(
    field: str,
    baseline: Mapping[str, JsonValue],
    deployments: Sequence[Mapping[str, object]],
    primary: ExternalMetadataRecord | None,
    secondary: ExternalMetadataRecord | None,
) -> _FieldChoice:
    if _overridden(field, deployments):
        return _FieldChoice(field=field, value=_baseline_value(baseline, field), source="deployment")
    preferred: Final = (secondary, primary) if field == "description" else (primary, secondary)
    candidates: Final = tuple(record for record in preferred if _record_value(record, field) is not None)
    if candidates:
        record: Final = candidates[0]
        return _FieldChoice(field=field, value=_record_value(record, field), source=record.source, record=record)
    return _FieldChoice(field=field, value=_baseline_value(baseline, field), source="litellm")


def _matching_secondary(
    identifier: str,
    primary: ExternalMetadataRecord | None,
    index: Mapping[tuple[str, str], ExternalMetadataRecord],
) -> ExternalMetadataRecord | None:
    direct: Final = index.get(("openrouter", identifier))
    if direct is not None:
        return direct
    aliases: Final = primary.aliases if primary is not None else ()
    matches: Final = tuple(index[("openrouter", alias)] for alias in aliases if ("openrouter", alias) in index)
    return matches[0] if len(matches) == 1 else None


def _provenance(source: str, choices: tuple[_FieldChoice, ...], public_id: str) -> MetadataSource | None:
    selected: Final = tuple(choice for choice in choices if choice.source == source and choice.value is not None)
    if not selected:
        return None
    record: Final = next((choice.record for choice in selected if choice.record is not None), None)
    return MetadataSource.model_validate(
        {
            "source": source,
            "url": record.source_url if record else
                "https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json"
                if source == "litellm" else None,
            "retrieved_at": record.retrieved_at if record else None,
            "fields": tuple(choice.field for choice in selected),
            "model_id": record.id if record else public_id,
        }
    )


def resolve_metadata(
    public_id: str,
    deployments: Sequence[Mapping[str, object]],
    baseline: Mapping[str, JsonValue],
    index: Mapping[tuple[str, str], ExternalMetadataRecord],
) -> MetadataResolution:
    identities: Final = tuple(dict.fromkeys(_canonical(public_id, item) for item in deployments))
    identifier: Final = identities[0] if len(identities) == 1 else None
    primary: Final = index.get(("models_dev", identifier)) if identifier else None
    secondary: Final = _matching_secondary(identifier, primary, index) if identifier else None
    capability_keys: Final = tuple(as_mapping(baseline.get("capabilities")))
    fields: Final = (*_SCALARS, "context_window", "name", *(f"capabilities.{key}" for key in capability_keys))
    choices: Final = tuple(_choose(field, baseline, deployments, primary, secondary) for field in fields)
    values: Final = {choice.field: choice.value for choice in choices}
    conflicts: Final = tuple(
        field for field in fields
        if _record_value(primary, field) is not None
        and _record_value(secondary, field) is not None
        and _record_value(primary, field) != _record_value(secondary, field)
        and field not in {"description", "name", "provider"}
    )
    return MetadataResolution(
        values={
            **{field: values[field] for field in _SCALARS if values[field] is not None},
            "capabilities": {key: values[f"capabilities.{key}"] for key in capability_keys},
        },
        name=values["name"] if isinstance(values["name"], str) else None,
        context_window=values["context_window"] if isinstance(values["context_window"], int) else None,
        sources=tuple(
            item for source in ("models_dev", "openrouter", "deployment", "litellm")
            if (item := _provenance(source, choices, public_id)) is not None
        ),
        conflicts=(*conflicts, *(("model_identity",) if len(identities) > 1 else ())),
    )
