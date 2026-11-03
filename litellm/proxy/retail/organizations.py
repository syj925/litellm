from collections.abc import Mapping
from datetime import datetime
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import JsonValue

from .api import Admin, OperationKey, User, _execute
from .control_plane_access import verify_actor
from .control_plane_flags import operation_audit_enabled, organizations_enabled
from .control_plane_http import PrivateManagementRoute
from .control_plane_schema import ControlPlaneStore as Store
from .organization_store import (
    MemberAssociate,
    MemberUpdate,
    OrganizationCreate,
    OrganizationStore,
    OrganizationUpdate,
    require_admin,
)


def _organizations_enabled() -> None:
    if not organizations_enabled():
        raise HTTPException(404, "This feature is not enabled")


def _audit_enabled() -> None:
    if not operation_audit_enabled():
        raise HTTPException(404, "This feature is not enabled")


router: Final = APIRouter(
    prefix="/retail",
    tags=["Retail organizations"],
    route_class=PrivateManagementRoute,
)
_ORGANIZATIONS: Final = [Depends(_organizations_enabled)]
_AUDIT: Final = [Depends(_audit_enabled)]


@router.get("/admin/organizations", dependencies=_ORGANIZATIONS)
async def admin_organizations(
    user: Admin,
    store: Store,
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(OrganizationStore(store).list(actor, admin=True, q=q, limit=limit, offset=offset))


@router.post("/admin/organizations", dependencies=_ORGANIZATIONS, status_code=201)
async def create_organization(
    data: OrganizationCreate, user: Admin, store: Store, key: OperationKey
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(OrganizationStore(store).create(data, actor, key))


@router.get("/admin/organizations/{organization_id}", dependencies=_ORGANIZATIONS)
async def admin_organization(organization_id: str, user: Admin, store: Store) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    return await _execute(OrganizationStore(store).get(organization_id, actor))


@router.patch("/admin/organizations/{organization_id}", dependencies=_ORGANIZATIONS)
async def update_organization(
    organization_id: str, data: OrganizationUpdate, user: Admin, store: Store, key: OperationKey
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(OrganizationStore(store).update(organization_id, data, actor, key))


@router.get("/organizations", dependencies=_ORGANIZATIONS)
async def own_organizations(
    user: User,
    store: Store,
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(OrganizationStore(store).list(actor, q=q, limit=limit, offset=offset))


@router.get("/organizations/{organization_id}", dependencies=_ORGANIZATIONS)
async def own_organization(organization_id: str, user: User, store: Store) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(OrganizationStore(store).get(organization_id, actor))


@router.get("/organizations/{organization_id}/members", dependencies=_ORGANIZATIONS)
async def organization_members(
    organization_id: str,
    user: User,
    store: Store,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(OrganizationStore(store).members(organization_id, actor, limit=limit, offset=offset))


@router.post("/organizations/{organization_id}/members", dependencies=_ORGANIZATIONS, status_code=201)
async def associate_organization_member(
    organization_id: str, data: MemberAssociate, user: User, store: Store, key: OperationKey
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(OrganizationStore(store).associate(organization_id, data, actor, key))


@router.patch("/organizations/{organization_id}/members/{user_id}", dependencies=_ORGANIZATIONS)
async def update_organization_member(
    organization_id: str, user_id: str, data: MemberUpdate, user: User, store: Store, key: OperationKey
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(OrganizationStore(store).update_member(organization_id, user_id, data, actor, key))


@router.get("/admin/operation-events", dependencies=_AUDIT)
async def admin_operation_events(
    user: Admin,
    store: Store,
    organization_id: str | None = Query(default=None, max_length=80),
    action: str | None = Query(default=None, max_length=100),
    actor_id: str | None = Query(default=None, max_length=191),
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    return await _execute(
        OrganizationStore(store).admin_events(
            actor,
            organization_id=organization_id,
            action=action,
            actor_id=actor_id,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/organizations/{organization_id}/operation-events", dependencies=_ORGANIZATIONS)
async def organization_operation_events(
    organization_id: str,
    user: User,
    store: Store,
    action: str | None = Query(default=None, max_length=100),
    actor_id: str | None = Query(default=None, max_length=191),
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    return await _execute(
        OrganizationStore(store).events(
            organization_id, actor, action=action, actor_id=actor_id, start=start, end=end, limit=limit, offset=offset
        )
    )
