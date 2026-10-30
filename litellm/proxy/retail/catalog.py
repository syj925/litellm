from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from itertools import groupby
from operator import attrgetter
from typing import Final, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from litellm.litellm_core_utils.get_model_cost_map import GetModelCostMap

from .api import Store, User
from .catalog_enrichment import MetadataSource, resolve_metadata
from .catalog_source_data import BenchmarkRecord
from .catalog_sources import ExternalMetadataRecord, read_records
from .pool_pricing import _PoolFactors, pool_discount_view
from .pool_store import (
    PoolAccess,
    PoolStore,
    _live_bindings,
    _PoolRow,
    _StoredBinding,
    effective_account_grants,
    router_deployments,
)
from .pricing import _catalog_model, as_mapping, catalog_cache_write_tiers, catalog_snapshot
from .store import WalletStore, _many, _ModelPriceRow

router: Final = APIRouter(tags=["Model catalog"])


class CatalogRates(BaseModel):
    model_config = ConfigDict(frozen=True)

    input: str | None = None
    output: str | None = None
    cache_read: str | None = None
    cache_write: str | None = None
    cache_write_tiers: dict[str, str] = Field(default_factory=dict)


class CatalogPricing(BaseModel):
    model_config = ConfigDict(frozen=True)

    currency: Literal["USD"] = "USD"
    unit: Literal["million_tokens"] = "million_tokens"
    reference: CatalogRates | None = None
    customer: CatalogRates | None = None
    discount: str | None = None
    source_label: str | None = None
    version: int | None = None
    updated_at: str | None = None


class CatalogOffer(BaseModel):
    model_config = ConfigDict(frozen=True)

    pool_id: str
    pool_name: str
    source_kind: str
    available_to_me: bool
    pricing: CatalogPricing


class CatalogCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    vision: bool | None = None
    tools: bool | None = None
    reasoning: bool | None = None
    prompt_caching: bool | None = None
    structured_output: bool | None = None
    audio_input: bool | None = None
    audio_output: bool | None = None
    pdf_input: bool | None = None


class CatalogModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    provider: str | None = None
    description: str | None = None
    context_length: int | None = None
    max_output_tokens: int | None = None
    context_window: int | None = None
    mode: str | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    capabilities: CatalogCapabilities
    metadata_source: Literal["reference_catalog", "deployment"] | None = None
    metadata_sources: tuple[MetadataSource, ...] = ()
    metadata_conflicts: tuple[str, ...] = ()
    benchmarks: tuple[BenchmarkRecord, ...] = ()
    reference_pricing: CatalogRates | None = None
    available_to_me: bool
    offers: tuple[CatalogOffer, ...]


class CatalogPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[CatalogModel, ...]
    metadata_note: str = (
        "Model descriptions and specifications combine models.dev, OpenRouter, configured metadata and LiteLLM. "
        "Source declarations describe the model; channel support can vary and has not been tested by this catalog. "
        "Missing values are unknown, not evidence that a capability is unsupported. "
        "Base reference prices are listed in USD per million tokens. "
        "Context, cache duration, tools and other pricing variants can have separate rates."
    )


class _DiscountRow(_PoolFactors):
    model: str


class _Metadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str | None = None
    description: str | None = None
    context_length: int | None = None
    max_output_tokens: int | None = None
    mode: str | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    capabilities: CatalogCapabilities
    source: Literal["reference_catalog", "deployment"] | None = None


_CAPABILITY_FIELDS: Final = {
    "vision": "supports_vision",
    "tools": "supports_function_calling",
    "reasoning": "supports_reasoning",
    "prompt_caching": "supports_prompt_caching",
    "structured_output": "supports_response_schema",
    "audio_input": "supports_audio_input",
    "audio_output": "supports_audio_output",
    "pdf_input": "supports_pdf_input",
}
_RATE_FIELDS: Final = {
    "input": "input_cost_per_token",
    "output": "output_cost_per_token",
    "cache_read": "cache_read_input_token_cost",
    "cache_write": "cache_creation_input_token_cost",
}


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _modalities(value: object) -> tuple[str, ...]:
    return tuple(sorted({item for item in value if isinstance(item, str)})) if isinstance(value, (list, tuple)) else ()


def _provider(model: str, metadata: Mapping[str, object]) -> str | None:
    leaf: Final = model.rsplit("/", 1)[-1].lower()
    if leaf.startswith("claude"):
        return "anthropic"
    if leaf.startswith("gemini"):
        return "google"
    if leaf.startswith(("gpt-", "chatgpt-", "o1-", "o3-", "o4-")) or leaf in {"o1", "o3", "o4"}:
        return "openai"
    return _string(metadata.get("litellm_provider"))


def _declared(info: Mapping[str, object], entry: Mapping[str, object], key: str) -> object:
    value: Final = info.get(key)
    return value if value is not None else entry.get(key)


def _deployment_metadata(
    model: str,
    deployment: Mapping[str, object],
    catalog: Mapping[str, Mapping[str, object]],
) -> _Metadata:
    catalog_name: Final = _catalog_model(deployment, catalog)
    entry: Final = catalog[catalog_name] if catalog_name is not None else {}
    info: Final = as_mapping(deployment.get("model_info"))
    capability_values: Final = {field: _bool(_declared(info, entry, key)) for field, key in _CAPABILITY_FIELDS.items()}
    mode: Final = _string(_declared(info, entry, "mode"))
    declared_input: Final = _modalities(_declared(info, entry, "supported_modalities"))
    declared_output: Final = _modalities(_declared(info, entry, "supported_output_modalities"))
    input_fallback: Final = ("text", "image") if capability_values["vision"] is True else ("text",)
    has_metadata: Final = any(
        info.get(key) is not None
        for key in (*_CAPABILITY_FIELDS.values(), "mode", "max_input_tokens", "max_output_tokens")
    )
    return _Metadata(
        provider=_provider(model, entry),
        description=_string(entry.get("description")),
        context_length=_integer(_declared(info, entry, "max_input_tokens")),
        max_output_tokens=_integer(_declared(info, entry, "max_output_tokens")),
        mode=mode,
        input_modalities=declared_input or (input_fallback if mode in {"chat", "completion", "embedding"} else ()),
        output_modalities=declared_output or (("text",) if mode in {"chat", "completion"} else ()),
        capabilities=CatalogCapabilities.model_validate(capability_values),
        source="reference_catalog" if entry else "deployment" if has_metadata else None,
    )


def _same_value(values: Sequence[object]) -> object | None:
    first: Final = values[0] if values else None
    return first if all(value == first for value in values) else None


def _common_modalities(values: Sequence[tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(sorted(frozenset(values[0]).intersection(*values[1:]))) if values else ()


def _metadata_consensus(metadata: Sequence[_Metadata]) -> _Metadata:
    scalar: Final = ("provider", "description", "context_length", "max_output_tokens", "mode", "source")
    fields: Final = {key: _same_value(tuple(getattr(item, key) for item in metadata)) for key in scalar}
    capabilities: Final = {
        key: _same_value(tuple(getattr(item.capabilities, key) for item in metadata)) for key in _CAPABILITY_FIELDS
    }
    return _Metadata.model_validate(
        {
            **fields,
            "input_modalities": _common_modalities(tuple(item.input_modalities for item in metadata)),
            "output_modalities": _common_modalities(tuple(item.output_modalities for item in metadata)),
            "capabilities": capabilities,
        }
    )


def _per_million(value: object) -> str | None:
    if value is None:
        return None
    try:
        amount: Final = Decimal(str(value))
        return format(amount * 1_000_000, "f") if amount.is_finite() and amount >= 0 else None
    except InvalidOperation:
        return None


def _rates(card: Mapping[str, object] | None, catalog_name: str | None = None) -> CatalogRates | None:
    if card is None:
        return None
    values: Final = {
        field: None if field == "cache_write" and card.get("cache_write_price_published") is False
        else _per_million(card.get(key)) for field, key in _RATE_FIELDS.items()
    }
    tiers: Final = (
        as_mapping(card.get("cache_write_tiers"))
        if "cache_write_tiers" in card
        else catalog_cache_write_tiers(catalog_name or "", card)
    )
    tier_rates: Final = {
        key: amount for key, value in tiers.items() if (amount := _per_million(value)) is not None
    }
    return (
        CatalogRates.model_validate({**values, "cache_write_tiers": tier_rates})
        if any(value is not None for value in values.values()) or tier_rates else None
    )


def _discounted(rates: CatalogRates | None, discount: str | None) -> CatalogRates | None:
    if rates is None or discount is None:
        return None
    return CatalogRates.model_validate(
        {
            **{
                key: format(Decimal(value) * Decimal(discount), "f") if value is not None else None
                for key, value in rates.model_dump(exclude={"cache_write_tiers"}).items()
            },
            "cache_write_tiers": {
                key: format(Decimal(value) * Decimal(discount), "f")
                for key, value in rates.cache_write_tiers.items()
            },
        }
    )


def _reference_card(
    model: str,
    deployments: Sequence[Mapping[str, object]],
    stored_prices: Mapping[str, Mapping[str, object]],
    catalog: Mapping[str, Mapping[str, object]],
) -> Mapping[str, object] | None:
    if model in stored_prices:
        return _published_reference_card(stored_prices[model], catalog)
    try:
        return catalog_snapshot(model, deployments, "1", catalog)
    except ValueError:
        return None


def _published_reference_card(
    card: Mapping[str, object], catalog: Mapping[str, Mapping[str, object]],
) -> Mapping[str, object]:
    if card.get("cache_write_price_published") is not None or card.get("source") != "catalog":
        return card
    name: Final = card.get("catalog_model")
    reference: Final = catalog.get(name, {}) if isinstance(name, str) else {}
    return {**card, "cache_write_price_published": reference.get("cache_creation_input_token_cost") is not None}


def _reference_rates(
    model: str,
    deployments: Sequence[Mapping[str, object]],
    stored_prices: Mapping[str, Mapping[str, object]],
    catalog: Mapping[str, Mapping[str, object]],
) -> CatalogRates | None:
    if model in stored_prices:
        return _rates(_published_reference_card(stored_prices[model], catalog))
    if model in catalog:
        return _rates(catalog[model], model)
    names: Final = tuple(dict.fromkeys(_catalog_model(deployment, catalog) for deployment in deployments))
    if not names or any(name is None for name in names):
        return None
    candidates: Final = tuple(_rates(catalog[name], name) for name in names if name is not None)
    first: Final = candidates[0]
    return first if all(item == first for item in candidates) else None


def _offer(
    model: str,
    pool: _PoolRow,
    grants: Mapping[str, frozenset[str]],
    card: Mapping[str, object] | None,
    reference_rates: CatalogRates | None,
    discounts: Mapping[tuple[str, str], _DiscountRow],
) -> CatalogOffer:
    available: Final = model in grants.get(pool.id, frozenset())
    factors: Final = discounts.get((pool.id, model)) if available else None
    discount: Final = str(pool_discount_view(model, factors)["discount"]) if factors is not None else None
    rates: Final = _rates(card)
    return CatalogOffer(
        pool_id=pool.id,
        pool_name=pool.name,
        source_kind=pool.source_kind,
        available_to_me=available,
        pricing=CatalogPricing(
            reference=reference_rates,
            customer=_discounted(rates, discount),
            discount=discount,
            source_label="Configured reference price"
            if card and card.get("version")
            else "LiteLLM reference catalog"
            if reference_rates is not None
            else None,
            version=_integer(card.get("version")) if card else None,
            updated_at=_string(card.get("updated_at")) if card else None,
        ),
    )


def _model_entry(
    model: str,
    bindings: Sequence[_StoredBinding],
    pools: Sequence[_PoolRow],
    grants: Mapping[str, frozenset[str]],
    deployments: Mapping[tuple[str, str], Mapping[str, object]],
    prices: Mapping[str, Mapping[str, object]],
    discounts: Mapping[tuple[str, str], _DiscountRow],
    catalog: Mapping[str, Mapping[str, object]],
    public_sources: Sequence[Mapping[str, object]] | None = None,
    external_index: Mapping[tuple[str, str], ExternalMetadataRecord] | None = None,
) -> CatalogModel:
    identifiers: Final = tuple(dict.fromkeys(binding.deployment_id for binding in bindings))
    sources: Final = (
        tuple(public_sources)
        if public_sources is not None
        else tuple(deployments[(identifier, model)] for identifier in identifiers)
    )
    metadata: Final = _metadata_consensus(tuple(_deployment_metadata(model, item, catalog) for item in sources))
    resolution: Final = resolve_metadata(model, sources, metadata.model_dump(mode="json"), external_index or {})
    enriched: Final = _Metadata.model_validate({**metadata.model_dump(), **resolution.values})
    card: Final = _reference_card(model, sources, prices, catalog)
    reference_rates: Final = _reference_rates(model, sources, prices, catalog)
    pool_ids: Final = frozenset(binding.pool_id for binding in bindings)
    offers: Final = tuple(
        _offer(model, pool, grants, card, reference_rates, discounts) for pool in pools if pool.id in pool_ids
    )
    return CatalogModel(
        id=model,
        name=resolution.name or model,
        provider=enriched.provider,
        description=enriched.description,
        context_length=enriched.context_length,
        max_output_tokens=enriched.max_output_tokens,
        context_window=resolution.context_window,
        mode=enriched.mode,
        input_modalities=enriched.input_modalities,
        output_modalities=enriched.output_modalities,
        capabilities=enriched.capabilities,
        metadata_source=metadata.source,
        metadata_sources=resolution.sources,
        metadata_conflicts=resolution.conflicts,
        benchmarks=resolution.benchmarks,
        reference_pricing=reference_rates,
        available_to_me=any(offer.available_to_me for offer in offers),
        offers=offers,
    )


def build_model_catalog(
    pools: Sequence[_PoolRow],
    bindings: Sequence[_StoredBinding],
    access: PoolAccess,
    deployments: Sequence[Mapping[str, object]],
    prices: Sequence[Mapping[str, object]],
    discounts: Sequence[_DiscountRow],
    catalog: Mapping[str, Mapping[str, object]],
    *,
    mode: Literal["personal", "public"] = "personal",
    external_records: Sequence[ExternalMetadataRecord] = (),
) -> CatalogPage:
    live: Final = _live_bindings(bindings, deployments)
    grants: Final = (
        {grant.pool_id: frozenset(grant.models) for grant in effective_account_grants(access, pools, live)}
        if mode == "personal"
        else {}
    )
    visible_pools: Final = tuple(
        pool
        for pool in pools
        if pool.enabled and (pool.visibility == "public" if mode == "public" else pool.id in grants)
    )
    pool_map: Final = {pool.id: pool for pool in visible_pools}
    visible: Final = tuple(binding for binding in live if _visible_binding(binding, pool_map, grants, mode))
    binding_groups: Final = {
        model: tuple(group)
        for model, group in groupby(sorted(visible, key=attrgetter("model")), key=attrgetter("model"))
    }
    public_deployments: Final = (
        tuple(item for item in deployments if _listed_request_name(item)) if mode == "public" else ()
    )
    public_groups: Final = {
        model: tuple(group)
        for model, group in groupby(sorted(public_deployments, key=_request_name), key=_request_name)
    }
    model_names: Final = tuple(sorted(public_groups if mode == "public" else binding_groups))
    deployment_map: Final = {
        (str(as_mapping(item.get("model_info")).get("id")), str(item.get("model_name"))): item for item in deployments
    }
    price_map: Final = {str(item["model"]): item for item in prices}
    discount_map: Final = {(item.pool_id, item.model): item for item in discounts} if mode == "personal" else {}
    external_index: Final = {(item.source, item.id): item for item in external_records}
    return CatalogPage(
        items=tuple(
            _model_entry(
                model,
                binding_groups.get(model, ()),
                visible_pools,
                grants,
                deployment_map,
                price_map,
                discount_map,
                catalog,
                public_groups.get(model) if mode == "public" else None,
                external_index,
            )
            for model in model_names
        )
    )


def _request_name(deployment: Mapping[str, object]) -> str:
    value: Final = deployment.get("model_name")
    return value if isinstance(value, str) and value.strip() else ""


def _listed_request_name(deployment: Mapping[str, object]) -> bool:
    value: Final = _request_name(deployment)
    return bool(value) and "*" not in value


def _visible_binding(
    binding: _StoredBinding,
    pools: Mapping[str, _PoolRow],
    grants: Mapping[str, frozenset[str]],
    mode: Literal["personal", "public"],
) -> bool:
    pool: Final = pools.get(binding.pool_id)
    return pool is not None and (
        pool.visibility == "public" if mode == "public" else binding.model in grants.get(binding.pool_id, frozenset())
    )


async def model_catalog(user_id: str, store: WalletStore) -> CatalogPage:
    pools_store: Final = PoolStore(store)
    deployments: Final = router_deployments()
    async with pools_store._read_transaction() as connection:
        pools, bindings = await pools_store._state(connection)
        access: Final = await pools_store._access(connection, user_id)
        prices: Final = await _many(connection, "SELECT * FROM retail_model_prices", (), _ModelPriceRow)
        discounts: Final = await _many(
            connection,
            """
            SELECT b.pool_id,b.model,w.user_id,w.plan_id,r.name AS pool_name,
                r.default_discount AS pool_default_discount,
                c.discount AS customer_discount,d.discount AS plan_discount,
                a.discount AS customer_pool_discount,p.discount AS plan_pool_discount
            FROM (SELECT DISTINCT pool_id,model FROM retail_pool_bindings) b
            JOIN retail_resource_pools r ON r.id=b.pool_id
            JOIN retail_wallets w ON w.user_id=%s
            LEFT JOIN retail_customer_pool_model_discounts c
                ON c.user_id=w.user_id AND c.pool_id=b.pool_id AND c.model=b.model
            LEFT JOIN retail_plan_pool_model_discounts d
                ON d.plan_id=w.plan_id AND d.pool_id=b.pool_id AND d.model=b.model
            LEFT JOIN retail_customer_pool_discounts a ON a.user_id=w.user_id AND a.pool_id=b.pool_id
            LEFT JOIN retail_plan_pool_discounts p ON p.plan_id=w.plan_id AND p.pool_id=b.pool_id
            """,
            (user_id,),
            _DiscountRow,
        )
    return build_model_catalog(
        pools,
        bindings,
        access,
        deployments,
        tuple(item.model_dump(mode="json") for item in prices),
        discounts,
        GetModelCostMap.loaded_model_cost_map(),
        external_records=await read_records(store),
    )


async def public_model_catalog(store: WalletStore) -> CatalogPage:
    pools_store: Final = PoolStore(store)
    deployments: Final = router_deployments()
    async with pools_store._read_transaction() as connection:
        pools, bindings = await pools_store._state(connection)
        prices: Final = await _many(connection, "SELECT * FROM retail_model_prices", (), _ModelPriceRow)
    return build_model_catalog(
        pools,
        bindings,
        PoolAccess(),
        deployments,
        tuple(item.model_dump(mode="json") for item in prices),
        (),
        GetModelCostMap.loaded_model_cost_map(),
        mode="public",
        external_records=await read_records(store),
    )


@router.get("/model-catalog", response_model=CatalogPage)
async def own_model_catalog(user: User, store: Store, response: Response) -> CatalogPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await model_catalog(user.user_id or "", store)
    except psycopg.Error as exc:
        raise HTTPException(503, "The model catalog is temporarily unavailable") from exc


@router.get("/public/model-catalog", response_model=CatalogPage)
async def public_catalog(store: Store, response: Response) -> CatalogPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await public_model_catalog(store)
    except psycopg.Error as exc:
        raise HTTPException(503, "The model catalog is temporarily unavailable") from exc
