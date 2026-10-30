from datetime import datetime, timezone
from typing import Final

from pydantic import JsonValue

from litellm.proxy.retail.catalog_source_data import normalize_models_dev, normalize_openrouter

_NOW: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_models_dev_keeps_model_facts_separate_from_own_provider_service_and_ignores_costs():
    models: Final[dict[str, JsonValue]] = {
        "lab/model": {
            "id": "lab/model",
            "name": "Readable model",
            "description": "Model description",
            "limit": {"input": 100, "context": 150, "output": 50},
            "modalities": {"input": ["text", "image/png"], "output": ["text"]},
            "reasoning": False,
            "tool_call": True,
        }
    }
    providers: Final[dict[str, JsonValue]] = {
        "lab": {
            "models": {
                "model": {
                    "canonical_model_id": "lab/model",
                    "structured_output": True,
                    "prompt_caching": False,
                    "cost": {"input": 777},
                }
            }
        },
        "reseller": {"models": {"model": {"structured_output": False}}},
        "openrouter": {
            "models": {
                "lab/alternate-id": {"canonical_model_id": "lab/model"},
                "lab/model:variant": {"canonical_model_id": "lab/model"},
            }
        },
    }
    record: Final = normalize_models_dev(models, providers, _NOW)[0]
    assert record.context_length == models["lab/model"]["limit"]["input"]
    assert record.context_window == models["lab/model"]["limit"]["context"]
    assert record.capabilities["reasoning"] is False
    assert record.capabilities["tools"] is True
    assert record.capabilities["structured_output"] is True
    assert record.capabilities["prompt_caching"] is False
    assert record.input_modalities == ("image", "text")
    assert record.aliases == ("lab/alternate-id",)
    assert "cost" not in record.model_dump()


def test_models_dev_does_not_promote_reseller_service_support_or_total_context_to_input_limit():
    models: Final[dict[str, JsonValue]] = {
        "lab/model": {"limit": {"context": 150}, "capabilities": {"tools": {"supported": True}}}
    }
    providers: Final[dict[str, JsonValue]] = {
        "reseller": {"models": {"model": {"structured_output": True, "prompt_caching": True}}},
        "lab": {"models": {"model": {"canonical_model_id": "lab/other", "structured_output": True}}},
    }
    record: Final = normalize_models_dev(models, providers, _NOW)[0]
    assert record.context_length is None
    assert record.context_window == 150
    assert record.capabilities["structured_output"] is None
    assert record.capabilities["prompt_caching"] is None
    assert record.capabilities["tools"] is True
    assert record.capabilities["vision"] is None


def test_openrouter_normalizes_explicit_parameters_without_inventing_absent_capabilities():
    document: Final[dict[str, JsonValue]] = {
        "data": [
            {
                "id": "lab/model",
                "description": "Catalog description",
                "context_length": 150,
                "architecture": {"input_modalities": ["text", "application/pdf"], "output_modalities": ["text"]},
                "top_provider": {"max_completion_tokens": 50},
                "supported_parameters": ["tools", "response_format", "reasoning_effort"],
                "pricing": {"prompt": "999", "input_cache_read": "1"},
                "latency": 0.01,
                "success_rate": 1,
            }
        ]
    }
    record: Final = normalize_openrouter(document, _NOW)[0]
    assert record.description == "Catalog description"
    assert record.context_window == 150
    assert record.context_length is None
    assert record.max_output_tokens == 50
    assert record.capabilities["tools"] is True
    assert record.capabilities["reasoning"] is True
    assert record.capabilities["structured_output"] is None
    assert record.capabilities["prompt_caching"] is None
    assert record.capabilities["pdf_input"] is True
    assert not {"pricing", "latency", "success_rate"}.intersection(record.model_dump())


def test_invalid_entries_are_omitted_and_missing_descriptions_are_unknown():
    document: Final[dict[str, JsonValue]] = {"data": [None, {}, {"id": "missing-slash"}, {"id": "lab/model"}]}
    records: Final = normalize_openrouter(document, _NOW)
    assert len(records) == 1
    assert records[0].description is None
    assert records[0].input_modalities == ()
    assert records[0].capabilities["vision"] is None


def test_generic_file_input_does_not_assert_pdf_is_unsupported():
    document: Final[dict[str, JsonValue]] = {
        "data": [{"id": "lab/model", "architecture": {"input_modalities": ["file", "text"]}}]
    }
    record: Final = normalize_openrouter(document, _NOW)[0]
    assert record.capabilities["pdf_input"] is None
