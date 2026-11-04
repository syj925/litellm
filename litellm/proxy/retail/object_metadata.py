from collections.abc import Mapping
from typing import Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import JsonValue

from .api import Admin, OperationKey, _execute
from .control_plane_access import verify_actor
from .control_plane_flags import object_metadata_enabled
from .control_plane_http import PrivateManagementRoute
from .control_plane_schema import ControlPlaneStore as Store
from .object_metadata_store import MetadataUpdate, ObjectMetadataStore
from .object_metadata_targets import ObjectKind


def _enabled() -> None:
    if not object_metadata_enabled():
        raise HTTPException(404, "This feature is not enabled")


router: Final = APIRouter(
    prefix="/retail/admin/object-metadata",
    tags=["Retail business labels"],
    route_class=PrivateManagementRoute,
    dependencies=[Depends(_enabled)],
)


@router.get("/targets")
async def targets(
    kind: ObjectKind,
    user: Admin,
    store: Store,
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(ObjectMetadataStore(store).targets(kind, actor, q=q, limit=limit, offset=offset))


@router.get("/{kind}/{reference_id}")
async def object_metadata(kind: ObjectKind, reference_id: UUID, user: Admin, store: Store) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(ObjectMetadataStore(store).get(kind, reference_id, actor))


@router.patch("/{kind}/{reference_id}")
async def update_object_metadata(
    kind: ObjectKind, reference_id: UUID, data: MetadataUpdate, user: Admin, store: Store, key: OperationKey
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(ObjectMetadataStore(store).update(kind, reference_id, data, actor, key))
