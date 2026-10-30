import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from itertools import chain
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .catalog_expression import _parse_rate, catalog_expression_policy, unsupported_catalog_pricing_metadata
from .pricing import (
    _catalog_card,
    _catalog_model,
    _published_reference_card,
    _rate,
    as_mapping,
    catalog_candidates,
    catalog_snapshot,
)


class PricingCatalogPrice(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    input_per_million: str
    output_per_million: str
    cache_read_per_million: str
    cache_write_per_million: str
    cache_write_tiers: dict[str, str] = Field(default_factory=dict)
    cache_write_price_published: bool | None = None
    expression_policy: dict[str, JsonValue] | None = None
    catalog_model: str | None = None
    source_label: str
    source: Literal["manual", "catalog", "legacy"] = "catalog"
    version: int | None = None
    updated_at: str | None = None


class PricingReviewReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    catalog_model: str
    source_label: str
    rates: dict[str, str]
    base_price: PricingCatalogPrice | None
    expression_policy: dict[str, JsonValue] | None = None


class PricingCatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    providers: tuple[str, ...]
    modes: tuple[str, ...]
    configured: bool
    status: Literal["expression", "custom", "catalog", "default", "ambiguous", "unavailable"]
    price: PricingCatalogPrice | None
    candidates: tuple[PricingCatalogPrice, ...]
    review_references: tuple[PricingReviewReference, ...] = ()
    reason: str | None = None


class PricingCatalogPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[PricingCatalogEntry, ...]


def _million_rate(value: object) -> str:
    return format(Decimal(str(value)) * 1_000_000, "f")


def _candidate_price(card: Mapping[str, JsonValue]) -> PricingCatalogPrice:
    return PricingCatalogPrice(
        model=str(card["model"]),
        catalog_model=str(card["catalog_model"]),
        source_label=str(card["source_label"]),
        input_per_million=_million_rate(card["input_cost_per_token"]),
        output_per_million=_million_rate(card["output_cost_per_token"]),
        cache_read_per_million=_million_rate(card["cache_read_input_token_cost"]),
        cache_write_per_million=_million_rate(card["cache_creation_input_token_cost"]),
        cache_write_tiers={key: _million_rate(value) for key, value in as_mapping(card.get("cache_write_tiers")).items()},
        cache_write_price_published=card.get("cache_write_price_published") is True,
        expression_policy={key: value for key, value in as_mapping(card["expression_policy"]).items()}
        if card.get("expression_policy") is not None else None,
    )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _review_reference(
    model: str,
    name: str,
    metadata: Mapping[str, object],
    source_label: str,
) -> PricingReviewReference:
    entries: Final = ((key, _rate(value)) for key, value in metadata.items() if "cost" in key)
    base_fields: Final = frozenset({
        "mode", "litellm_provider", "input_cost_per_token", "output_cost_per_token",
        "cache_read_input_token_cost", "cache_creation_input_token_cost",
        "cache_creation_input_token_cost_above_1hr", "cache_creation_input_token_cost_above_12hr",
        "cache_write_tiers",
    })
    base_metadata: Final = {
        key: value for key, value in metadata.items()
        if key in base_fields
    }
    base: Final = (
        _catalog_card(name, base_metadata) if metadata.get("mode") in {"chat", "completion", "embedding"} else None
    )
    return PricingReviewReference(
        catalog_model=name,
        source_label=source_label,
        rates={key: format(rate, "f") for key, rate in entries if rate is not None},
        base_price=_candidate_price({"model": model, "source_label": source_label, **base}) if base else None,
        expression_policy=catalog_expression_policy(name, metadata),
    )


def _review_references(
    model: str,
    deployments: Sequence[Mapping[str, object]],
    saved: PricingCatalogPrice | None,
    catalog: Mapping[str, Mapping[str, object]],
    source_label: str,
) -> tuple[PricingReviewReference, ...]:
    sources: Final = deployments or (
        {"litellm_params": {"model": saved.catalog_model or model}} if saved else {"litellm_params": {"model": model}},
    )
    resolved: Final = tuple(_catalog_model(deployment, catalog) for deployment in sources)
    names: Final = tuple(dict.fromkeys(name for name in resolved if name is not None))
    return tuple(_review_reference(model, name, catalog[name], source_label) for name in names)


def _deployment_category(
    deployment: Mapping[str, object],
    catalog: Mapping[str, Mapping[str, object]],
) -> tuple[str | None, str | None]:
    name: Final = _catalog_model(deployment, catalog)
    metadata: Final = catalog.get(name, {}) if name else {}
    params: Final = as_mapping(deployment.get("litellm_params"))
    info: Final = as_mapping(deployment.get("model_info"))
    request_model: Final = _string(params.get("model")) or ""
    prefix: Final = request_model.partition("/")[0] if "/" in request_model else None
    return (
        _string(metadata.get("litellm_provider")) or _string(params.get("custom_llm_provider")) or prefix,
        _string(metadata.get("mode")) or _string(info.get("mode")),
    )


def _categories(
    model: str,
    deployments: Sequence[Mapping[str, object]],
    saved: PricingCatalogPrice | None,
    catalog: Mapping[str, Mapping[str, object]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    sources: Final = deployments or (
        {"litellm_params": {"model": saved.catalog_model or model}} if saved else {"litellm_params": {"model": model}},
    )
    categories: Final = tuple(_deployment_category(deployment, catalog) for deployment in sources)
    return (
        tuple(sorted({provider for provider, _ in categories if provider})),
        tuple(sorted({mode for _, mode in categories if mode})),
    )


def _unresolved_pricing_field(key: str, value: object, metadata: Mapping[str, object]) -> bool:
    if value is None or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,119}", key) is None:
        return False
    if unsupported_catalog_pricing_metadata({key: value}):
        return True
    if "cost" not in key or _parse_rate(key, value) is not None:
        return False
    if key in {"input_cost_per_image", "output_cost_per_image"}:
        return metadata.get("mode") not in {"image_generation", "image_edit"}
    if key in {"search_context_cost_per_query", "google_maps_grounding_cost_per_query"}:
        from .catalog_image_expression import catalog_search_expression_policy

        search_metadata: Final = {
            **{name: metadata[name] for name in ("litellm_provider", "web_search_billing_unit") if name in metadata},
            key: value,
        }
        return catalog_search_expression_policy(
            {"version": "expression-v1", "expression": "0", "timezone": "UTC", "usage_bounds": {}}, search_metadata,
        ) is None
    return True


def _unresolved_reference_fields(metadata: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(key for key, value in metadata.items() if _unresolved_pricing_field(key, value, metadata))


def _pricing_review_reason(
    references: Sequence[PricingReviewReference], catalog: Mapping[str, Mapping[str, object]],
) -> str | None:
    fields: Final = tuple(sorted(frozenset(chain.from_iterable(
        _unresolved_reference_fields(catalog[reference.catalog_model]) for reference in references
    ))))
    if not fields:
        return None
    suffix: Final = f"; {len(fields) - 16} more fields" if len(fields) > 16 else ""
    return "Manual pricing is required for unresolved catalog fields: " + ", ".join(fields[:16]) + suffix


def _catalog_entry(
    model: str,
    deployments: Sequence[Mapping[str, object]],
    saved: PricingCatalogPrice | None,
    catalog: Mapping[str, Mapping[str, object]],
    source_label: str,
) -> PricingCatalogEntry:
    candidates: Final = tuple(_candidate_price(card) for card in catalog_candidates(model, deployments, catalog, source_label))
    review_references: Final = _review_references(model, deployments, saved, catalog, source_label)
    providers, modes = _categories(model, deployments, saved, catalog)
    if saved is not None:
        status: Final = (
            "expression" if saved.expression_policy is not None else "catalog" if saved.source == "catalog" else "custom"
        )
        return PricingCatalogEntry(
            model=model, providers=providers, modes=modes, configured=bool(deployments),
            status=status,
            price=saved, candidates=candidates,
            review_references=review_references,
        )
    try:
        catalog_snapshot(model, deployments, "1", catalog)
    except ValueError:
        return PricingCatalogEntry(
            model=model, providers=providers, modes=modes, configured=bool(deployments),
            status="ambiguous" if candidates else "unavailable", price=None, candidates=candidates,
            review_references=review_references,
            reason=_pricing_review_reason(review_references, catalog) or (
                "Deployments do not share one supported catalog reference. Choose a reference or set a custom base price."
                if candidates else
                "No supported flat token price is available. Unknown, tiered and non-token prices require manual review."
            ),
        )
    return PricingCatalogEntry(
        model=model, providers=providers, modes=modes, configured=bool(deployments),
        status="expression" if candidates[0].expression_policy is not None else "default",
        price=candidates[0], candidates=candidates,
        review_references=review_references,
    )


def build_pricing_catalog(
    configured: Mapping[str, Sequence[Mapping[str, object]]],
    saved: Sequence[PricingCatalogPrice],
    catalog: Mapping[str, Mapping[str, object]],
    source_label: str,
) -> PricingCatalogPage:
    overrides: Final = {
        price.model: PricingCatalogPrice.model_validate(_published_reference_card(price.model_dump(), catalog))
        for price in saved
    }
    return PricingCatalogPage(items=tuple(
        _catalog_entry(model, configured.get(model, ()), overrides.get(model), catalog, source_label)
        for model in sorted(configured.keys() | overrides.keys())
    ))
