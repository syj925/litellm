import asyncio
from collections.abc import Sequence
from typing import Final
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from litellm.proxy._types import ModelInfoDelete, UserAPIKeyAuth
from litellm.types.router import Deployment

from .api import Admin, Store, _execute
from .channel_store import ChannelDetails, ChannelMember, ChannelStore, ChannelUpdate
from .pool_store import deployment_catalog, router_deployments
from .store import WalletStore

router: Final = APIRouter(prefix="/admin/channels", tags=["Channels"])


class ChannelCreate(ChannelDetails):
    deployment_ids: tuple[str, ...] = Field(default=(), max_length=2000)
    deployments: tuple[Deployment, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def one_source(self) -> "ChannelCreate":
        if self.deployment_ids and self.deployments:
            raise ValueError("Choose existing deployments or create new deployments in one operation")
        ChannelUpdate(name=self.name, pool_id=self.pool_id, deployment_ids=self.deployment_ids)
        _validate_new_deployments(self.deployments)
        return self


class ChannelAddDeployments(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    deployments: tuple[Deployment, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def new_ids_only(self) -> "ChannelAddDeployments":
        _validate_new_deployments(self.deployments)
        return self


class ChannelEdit(ChannelUpdate):
    expected_version: int = Field(ge=1)


def _validate_new_deployments(deployments: Sequence[Deployment]) -> None:
    if any(item.model_info.team_id for item in deployments):
        raise ValueError("Resource channels require gateway deployments without a team-specific alias")
    if len({item.model_name for item in deployments}) != len(deployments):
        raise ValueError("Each channel batch must contain distinct public model names")


def prepare_channel_deployments(deployments: Sequence[Deployment]) -> tuple[Deployment, ...]:
    return tuple(
        item.model_copy(update={"model_info": item.model_info.model_copy(update={"id": str(uuid4())})})
        for item in deployments
    )


@router.get("")
async def admin_channels(admin: Admin, store: Store) -> dict[str, JsonValue]:
    return await _execute(ChannelStore(store).list_channels())


@router.post("")
async def admin_create_channel(data: ChannelCreate, admin: Admin, store: Store) -> dict[str, JsonValue]:
    if data.deployments:
        return await _execute(_provision_channel(data, data.deployments, admin, store))
    return await _execute(
        ChannelStore(store).save_existing(
            ChannelUpdate(name=data.name, pool_id=data.pool_id, deployment_ids=data.deployment_ids),
            router_deployments(),
        )
    )


@router.put("/{channel_id}")
async def admin_update_channel(channel_id: str, data: ChannelEdit, admin: Admin, store: Store) -> dict[str, JsonValue]:
    return await _execute(ChannelStore(store).save_existing(data, router_deployments(), channel_id))


@router.post("/{channel_id}/deployments")
async def admin_add_channel_deployments(
    channel_id: str, data: ChannelAddDeployments, admin: Admin, store: Store
) -> dict[str, JsonValue]:
    page: Final = await _execute(ChannelStore(store).list_channels())
    items: Final = page.get("items")
    found: Final = (
        next((item for item in items if isinstance(item, dict) and item.get("id") == channel_id), None)
        if isinstance(items, list)
        else None
    )
    if found is None:
        raise HTTPException(404, "Channel does not exist")
    details: Final = ChannelDetails(name=str(found["name"]), pool_id=str(found["pool_id"]))
    return await _execute(_provision_channel(details, data.deployments, admin, store, channel_id))


@router.post("/{channel_id}/recover")
async def admin_recover_channel(channel_id: str, admin: Admin, store: Store) -> dict[str, JsonValue]:
    channels: Final = ChannelStore(store)
    async with channels.operation_lock(channel_id):
        identifiers: Final = await channels.pending_operation(channel_id)
        remaining: Final = await _remove_new_deployments(identifiers, admin)
        result: Final = await channels.finish_new(channel_id, identifiers, failed=True, cleanup_failed=remaining)
        if remaining:
            raise HTTPException(
                503, "Some pending deployments could not be removed. The channel remains locked for review"
            )
        return result


async def _provision_channel(
    details: ChannelDetails,
    deployments: Sequence[Deployment],
    admin: UserAPIKeyAuth,
    store: WalletStore,
    channel_id: str | None = None,
) -> dict[str, JsonValue]:
    identifier: Final = channel_id or str(uuid4())
    channels: Final = ChannelStore(store)
    async with channels.operation_lock(identifier):
        return await _provision_locked(details, deployments, admin, channels, identifier, channel_id is None)


async def _provision_locked(
    details: ChannelDetails,
    deployments: Sequence[Deployment],
    admin: UserAPIKeyAuth,
    channels: ChannelStore,
    identifier: str,
    created: bool,
) -> dict[str, JsonValue]:
    from litellm.proxy.management_endpoints.model_management_endpoints import add_new_model

    prepared: Final = prepare_channel_deployments(deployments)
    members: Final = tuple(
        ChannelMember(channel_id=identifier, deployment_id=str(item.model_info.id), model=item.model_name)
        for item in prepared
    )
    identifiers: Final = tuple(item.deployment_id for item in members)
    await channels.reserve_new(details, members, identifier)
    try:
        for deployment in prepared:
            await add_new_model(model_params=deployment, user_api_key_dict=admin)
        live: Final = frozenset(item.deployment_id for item in deployment_catalog(router_deployments()))
        if not set(identifiers).issubset(live):
            raise HTTPException(503, "The new deployments are not ready to serve")
        return await channels.finish_new(identifier, identifiers, failed=False)
    except BaseException as exc:
        remaining: Final = await _remove_new_deployments(identifiers, admin)
        await channels.finish_new(identifier, identifiers, failed=True, cleanup_failed=remaining, created=created)
        if not isinstance(exc, Exception):
            raise
        if remaining:
            raise HTTPException(
                503,
                {
                    "error": "Channel provisioning failed and needs administrator cleanup",
                    "channel_id": identifier,
                    "remaining_deployment_ids": list(remaining),
                },
            ) from exc
        raise HTTPException(400, "Channel creation failed. All newly created deployments were rolled back") from exc


async def _remove_new_deployments(identifiers: Sequence[str], admin: UserAPIKeyAuth) -> tuple[str, ...]:
    from litellm.proxy.management_endpoints.model_management_endpoints import delete_model
    from litellm.proxy.proxy_server import MODEL_RECONCILE_LOCK, llm_router, prisma_client, proxy_config
    from litellm.repositories.model_repository import ModelRepository

    if prisma_client is None:
        return tuple(identifiers)

    async def remove(identifier: str) -> str | None:
        try:
            row: Final = await ModelRepository(prisma_client).table.find_unique(where={"model_id": identifier})
            if row is not None:
                await delete_model(ModelInfoDelete(id=identifier), admin)
            else:
                async with MODEL_RECONCILE_LOCK:
                    proxy_config.remove_auto_router_catalog_entries(frozenset({identifier}))
                    if llm_router is not None:
                        llm_router.delete_deployment(id=identifier)
            return None
        except Exception:
            return identifier

    outcomes: Final = await asyncio.gather(*(remove(identifier) for identifier in identifiers))
    return tuple(item for item in outcomes if item is not None)
