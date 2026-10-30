from datetime import datetime, timezone
from typing import Final

from pydantic import JsonValue

from litellm.proxy.retail.catalog import build_model_catalog
from litellm.proxy.retail.catalog_enrichment import resolve_metadata
from litellm.proxy.retail.catalog_source_data import ExternalMetadataRecord
from litellm.proxy.retail.pool_store import PoolAccess


def _record(source: str, **values: object) -> ExternalMetadataRecord:
    return ExternalMetadataRecord.model_validate(
        {
            "source": source,
            "id": "anthropic/claude-example-1",
            "name": "Example One",
            "provider": "anthropic",
            "source_url": "https://models.dev/models.json" if source == "models_dev" else "https://openrouter.ai/models",
            "retrieved_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            **values,
        }
    )


def _deployment(upstream: str = "anthropic/claude-example-1", **info: object) -> dict[str, object]:
    return {
        "model_name": "public-alias",
        "litellm_params": {"model": upstream, "api_key": "private", "api_base": "https://supplier.invalid"},
        "model_info": {"id": "example", **info},
    }


def _baseline() -> dict[str, JsonValue]:
    return {
        "provider": "anthropic",
        "description": None,
        "context_length": 16000,
        "max_output_tokens": 2000,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "capabilities": {"tools": None, "reasoning": None, "pdf_input": None},
    }


def test_description_uses_openrouter_while_specs_use_primary_and_record_field_sources() -> None:
    primary: Final = _record("models_dev", description="Model facts", context_length=32000, context_window=40000)
    secondary: Final = _record("openrouter", description="Richer introduction", context_window=64000)
    result: Final = resolve_metadata(
        "public-alias", (_deployment(),), _baseline(),
        {(item.source, item.id): item for item in (primary, secondary)},
    )
    assert result.values["description"] == secondary.description
    assert result.values["context_length"] == primary.context_length
    assert result.context_window == primary.context_window
    provenance: Final = {item.source: item.fields for item in result.sources}
    assert "description" in provenance["openrouter"]
    assert "context_window" in provenance["models_dev"]
    assert "context_window" in result.conflicts
    assert "description" not in result.conflicts


def test_secondary_total_context_never_overwrites_input_limit_and_missing_capability_stays_unknown() -> None:
    secondary: Final = _record("openrouter", context_window=64000, capabilities={"tools": True})
    result: Final = resolve_metadata(
        "public-alias", (_deployment(),), _baseline(), {("openrouter", secondary.id): secondary}
    )
    assert result.context_window == secondary.context_window
    assert result.values["context_length"] == _baseline()["context_length"]
    assert result.values["capabilities"] == {"tools": True, "reasoning": None, "pdf_input": None}


def test_primary_false_is_not_promoted_by_secondary_true() -> None:
    primary: Final = _record("models_dev", capabilities={"tools": False})
    secondary: Final = _record("openrouter", capabilities={"tools": True})
    result: Final = resolve_metadata(
        "public-alias", (_deployment(),), _baseline(),
        {(item.source, item.id): item for item in (primary, secondary)},
    )
    assert result.values["capabilities"]["tools"] is False
    assert "capabilities.tools" in result.conflicts


def test_explicit_deployment_limit_and_capabilities_keep_priority() -> None:
    record: Final = _record("models_dev", context_length=32000, capabilities={"tools": True})
    baseline: Final = {**_baseline(), "context_length": 8000, "capabilities": {"tools": False}}
    result: Final = resolve_metadata(
        "public-alias", (_deployment(max_input_tokens=8000, supports_function_calling=False),), baseline,
        {("models_dev", record.id): record},
    )
    assert result.values["context_length"] == baseline["context_length"]
    assert result.values["capabilities"]["tools"] is False
    assert {"context_length", "capabilities.tools"}.issubset(
        next(item.fields for item in result.sources if item.source == "deployment")
    )


def test_only_declared_unambiguous_aliases_join_secondary() -> None:
    primary: Final = _record("models_dev", aliases=("anthropic/claude-example.1",))
    secondary: Final = _record("openrouter", id=primary.aliases[0], description="Mapped description")
    result: Final = resolve_metadata(
        "public-alias", (_deployment(),), _baseline(),
        {(item.source, item.id): item for item in (primary, secondary)},
    )
    assert result.values["description"] == secondary.description
    dated: Final = resolve_metadata(
        "public-alias", (_deployment("anthropic/claude-example-1-20260101"),), _baseline(),
        {(item.source, item.id): item for item in (primary, secondary)},
    )
    assert dated.values.get("description") is None


def test_public_alias_does_not_override_upstream_identity_or_expose_connection_details() -> None:
    record: Final = _record("models_dev", description="Wrong model")
    result: Final = resolve_metadata(
        "claude-example-1", (_deployment("anthropic/another-model"),), _baseline(),
        {("models_dev", record.id): record},
    )
    assert result.values.get("description") is None
    assert all(value not in result.model_dump_json() for value in ("supplier.invalid", "private", "another-model"))


def test_mixed_upstream_identities_do_not_receive_one_models_external_claims() -> None:
    record: Final = _record("models_dev", description="One model only")
    result: Final = resolve_metadata(
        "public-alias", (_deployment(), _deployment("anthropic/different-model")), _baseline(),
        {("models_dev", record.id): record},
    )
    assert result.values.get("description") is None
    assert "model_identity" in result.conflicts


def test_enrichment_preserves_exact_request_id_and_pricing() -> None:
    record: Final = _record("models_dev", description="External description", name="Display name")
    args: Final = ((), (), PoolAccess(), (_deployment(),), ({
        "model": "public-alias", "version": 1,
        "input_cost_per_token": "0.000001", "output_cost_per_token": "0.000002",
        "cache_read_input_token_cost": "0.0000001", "cache_creation_input_token_cost": "0.00000125",
    },), (), {})
    before: Final = build_model_catalog(*args, mode="public").items[0]
    after: Final = build_model_catalog(*args, mode="public", external_records=(record,)).items[0]
    assert after.id == before.id
    assert after.name == record.name
    assert after.reference_pricing == before.reference_pricing
    assert after.offers == before.offers
    assert all(value not in after.model_dump_json() for value in ("supplier.invalid", '"api_key"'))


def test_model_benchmarks_keep_source_and_protocol_without_averaging_or_alias_guessing() -> None:
    benchmark: Final = {
        "name": "Fixture benchmark", "score": 61.2, "metric": "accuracy", "source": "models_dev",
        "source_url": "https://benchmark.invalid/report", "retrieved_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "version": "v2", "harness": "fixture-harness",
    }
    primary: Final = _record("models_dev", benchmarks=(benchmark,))
    secondary: Final = _record("openrouter", benchmarks=({**benchmark, "source": "openrouter", "score": 64.0},))
    index: Final = {(item.source, item.id): item for item in (primary, secondary)}
    result: Final = resolve_metadata("public-alias", (_deployment(),), _baseline(), index)
    assert len(result.benchmarks) == 2
    assert result.benchmarks[0] is primary.benchmarks[0]
    assert result.benchmarks[1] is secondary.benchmarks[0]
    assert all(item.version == benchmark["version"] and item.harness == benchmark["harness"] for item in result.benchmarks)
    assert all("benchmarks" in item.fields for item in result.sources if item.source in {"models_dev", "openrouter"})
    mismatched: Final = resolve_metadata("public-alias", (_deployment("anthropic/other-model"),), _baseline(), index)
    assert mismatched.benchmarks == ()
