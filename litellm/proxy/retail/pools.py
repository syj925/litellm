from typing import Final

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from litellm.proxy._types import LitellmUserRoles

from .api import Admin, Store, User, _execute, _model_price_view, _StoredModelPrice, _validated_discount
from .catalog import router as catalog_router
from .catalog_sources import catalog_sources_lifespan
from .catalog_sources import router as catalog_sources_router
from .channel_store import ChannelStore
from .channels import router as channels_router
from .model_metrics import router as model_metrics_router
from .pool_pricing import pool_default_discount, set_pool_default_discount
from .pool_store import (
    PoolAccess,
    PoolBinding,
    PoolDefinition,
    PoolStore,
    ResourcePolicy,
    router_deployments,
)

router: Final = APIRouter(prefix="/retail", tags=["Resource pools"], lifespan=catalog_sources_lifespan)
router.include_router(channels_router)
router.include_router(catalog_router)
router.include_router(catalog_sources_router)
router.include_router(model_metrics_router)


class PoolUpsert(PoolDefinition):
    bindings: tuple[PoolBinding, ...] | None = Field(default=None, max_length=2000)


class BindingsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bindings: tuple[PoolBinding, ...] = Field(max_length=2000)


class SettingsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    routing_enabled: bool


class PreviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1, max_length=191)
    policy: ResourcePolicy = Field(default_factory=ResourcePolicy)
    user_id: str | None = Field(default=None, min_length=1, max_length=191)


class PoolDiscountRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    discount: str | None = Field(min_length=1, max_length=30)

    @field_validator("discount")
    @classmethod
    def valid_discount(cls, value: str | None) -> str | None:
        return _validated_discount(value) if value is not None else None


@router.get("/pools")
async def own_pools(user: User, store: Store) -> dict[str, JsonValue]:
    return await _execute(PoolStore(store).account_catalog(user.user_id or "", router_deployments()))


@router.get("/admin/pools")
async def admin_pools(admin: Admin, store: Store) -> dict[str, JsonValue]:
    return await _execute(PoolStore(store).list_pools())


@router.get("/admin/pools/deployments")
async def admin_pool_deployments(admin: Admin, store: Store) -> dict[str, JsonValue]:
    return await _execute(ChannelStore(store).catalog(router_deployments()))


@router.get("/admin/pools/settings")
async def admin_pool_settings(admin: Admin, store: Store) -> dict[str, JsonValue]:
    return await _execute(PoolStore(store).settings(router_deployments()))


@router.put("/admin/pools/settings")
async def admin_save_pool_settings(data: SettingsRequest, admin: Admin, store: Store) -> dict[str, JsonValue]:
    return await _execute(PoolStore(store).settings(router_deployments(), data.routing_enabled))


@router.get("/admin/pools/catalog")
async def admin_pool_catalog(
    admin: Admin, store: Store, user_id: str | None = Query(default=None, max_length=191)
) -> dict[str, JsonValue]:
    return await _execute(PoolStore(store).account_catalog(user_id or admin.user_id or "", router_deployments()))


@router.post("/admin/pools")
async def admin_create_pool(data: PoolUpsert, admin: Admin, store: Store) -> dict[str, JsonValue]:
    definition: Final = PoolDefinition.model_validate(data.model_dump(exclude={"bindings"}))
    return await _execute(PoolStore(store).save_pool(definition, router_deployments(), data.bindings, create_only=True))


@router.get("/admin/pools/{pool_id}")
async def admin_pool(pool_id: str, admin: Admin, store: Store) -> dict[str, JsonValue]:
    page: Final = await _execute(PoolStore(store).list_pools())
    items: Final = page.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id") == pool_id:
                return item
    raise HTTPException(404, "Resource pool does not exist")


@router.put("/admin/pools/{pool_id}")
async def admin_save_pool(pool_id: str, data: PoolUpsert, admin: Admin, store: Store) -> dict[str, JsonValue]:
    if pool_id != data.id:
        raise HTTPException(400, "The pool ID in the path and body must match")
    definition: Final = PoolDefinition.model_validate(data.model_dump(exclude={"bindings"}))
    return await _execute(PoolStore(store).save_pool(definition, router_deployments(), data.bindings, update_only=True))


@router.delete("/admin/pools/{pool_id}")
async def admin_disable_pool(pool_id: str, admin: Admin, store: Store) -> dict[str, JsonValue]:
    return await _execute(PoolStore(store).disable_pool(pool_id))


@router.put("/admin/pools/{pool_id}/bindings")
async def admin_pool_bindings(pool_id: str, data: BindingsRequest, admin: Admin, store: Store) -> dict[str, JsonValue]:
    return await _execute(PoolStore(store).replace_bindings(pool_id, data.bindings, router_deployments()))


@router.get("/admin/wallets/{user_id}/pool-access")
async def admin_account_pool_access(user_id: str, admin: Admin, store: Store) -> dict[str, JsonValue]:
    return await _execute(PoolStore(store).account_access(user_id, router_deployments()))


@router.put("/admin/wallets/{user_id}/pool-access")
async def admin_save_account_pool_access(
    user_id: str, data: PoolAccess, admin: Admin, store: Store
) -> dict[str, JsonValue]:
    return await _execute(PoolStore(store).account_access(user_id, router_deployments(), data))


@router.get("/admin/wallets/{user_id}/pool-discount")
async def admin_customer_pool_discount(
    user_id: str,
    admin: Admin,
    store: Store,
    pool_id: str = Query(min_length=1, max_length=80),
) -> dict[str, JsonValue]:
    return await _execute(pool_default_discount(store, pool_id, user_id=user_id))


@router.put("/admin/wallets/{user_id}/pool-discount")
async def admin_save_customer_pool_discount(
    user_id: str,
    data: PoolDiscountRequest,
    admin: Admin,
    store: Store,
    pool_id: str = Query(min_length=1, max_length=80),
) -> dict[str, JsonValue]:
    return await _execute(set_pool_default_discount(store, pool_id, data.discount, user_id=user_id))


@router.get("/admin/plans/{plan_id}/pool-discount")
async def admin_plan_pool_discount(
    plan_id: str,
    admin: Admin,
    store: Store,
    pool_id: str = Query(min_length=1, max_length=80),
) -> dict[str, JsonValue]:
    return await _execute(pool_default_discount(store, pool_id, plan_id=plan_id))


@router.put("/admin/plans/{plan_id}/pool-discount")
async def admin_save_plan_pool_discount(
    plan_id: str,
    data: PoolDiscountRequest,
    admin: Admin,
    store: Store,
    pool_id: str = Query(min_length=1, max_length=80),
) -> dict[str, JsonValue]:
    return await _execute(set_pool_default_discount(store, pool_id, data.discount, plan_id=plan_id))


@router.post("/pools/preview")
async def pool_preview(data: PreviewRequest, user: User, store: Store) -> dict[str, JsonValue]:
    target: Final = data.user_id or user.user_id or ""
    if target != user.user_id and user.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(403, "Only administrators may preview resource pools for another account")
    decision: Final = await PoolStore(store).resolve_route(
        target,
        data.model,
        data.policy.model_dump(mode="json", exclude_none=True),
        router_deployments(),
    )
    if decision is None:
        return {"pool_id": None, "legacy_bypass": True}
    await _execute(store.get_wallet(target, create=True))
    discount: Final = await _execute(store.get_model_discount(target, data.model, decision.pool_id))
    reference: Final = await store.get_model_price(data.model)
    prices: Final = _model_price_view(_StoredModelPrice.model_validate(reference)) if reference is not None else None
    visible: Final = {
        "pool_id": decision.pool_id,
        "pool_name": decision.pool_name,
        "model": data.model,
        "eligible_count": len(decision.allowed_deployment_ids),
        "settings_version": decision.settings_version,
        "discount": discount["discount"],
        "discount_source": discount["discount_source"],
        "reference_prices": prices,
    }
    return (
        {**decision.model_dump(mode="json"), **visible} if user.user_role == LitellmUserRoles.PROXY_ADMIN else visible
    )
