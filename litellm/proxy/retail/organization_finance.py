from collections.abc import Mapping
from datetime import datetime
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import JsonValue

from .api import Admin, OperationKey, _execute
from .budget_management import validate_user_id
from .control_plane_access import verify_actor, verify_target_user
from .control_plane_flags import organization_finance_enabled
from .control_plane_http import PrivateManagementRoute
from .control_plane_schema import ControlPlaneStore as Store
from .organization_finance_store import (
    BillingOrganizationUpdate,
    BillingOrganizationView,
    EligibleBillingOrganizations,
    OrganizationConsumption,
    OrganizationFinanceStore,
)
from .organization_store import require_admin


def _enabled() -> None:
    if not organization_finance_enabled():
        raise HTTPException(404, "This feature is not enabled")


def _queries(request: Request, allowed: frozenset[str]) -> None:
    if set(request.query_params) - allowed:
        raise HTTPException(422, "Unsupported organization finance query fields")


router: Final = APIRouter(
    prefix="/retail/admin", tags=["Retail organization consumption"],
    route_class=PrivateManagementRoute, dependencies=[Depends(_enabled)],
)


@router.get("/users/{user_id}/billing-organization", response_model=BillingOrganizationView)
async def billing_organization(request: Request, user_id: str, user: Admin, store: Store) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    _queries(request, frozenset())
    validate_user_id(user_id)
    await verify_target_user(user_id)
    return await _execute(OrganizationFinanceStore(store).get(user_id, actor))


@router.patch("/users/{user_id}/billing-organization", response_model=BillingOrganizationView)
async def update_billing_organization(
    request: Request, user_id: str, data: BillingOrganizationUpdate, user: Admin, store: Store, key: OperationKey
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    _queries(request, frozenset())
    validate_user_id(user_id)
    await verify_target_user(user_id)
    return await _execute(OrganizationFinanceStore(store).update(user_id, data, actor, key))


@router.get("/users/{user_id}/billing-organization/organizations", response_model=EligibleBillingOrganizations)
async def billing_organization_choices(
    request: Request, user_id: str, user: Admin, store: Store,
    q: str = Query(default="", max_length=100), limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    _queries(request, frozenset(("q", "limit", "offset")))
    validate_user_id(user_id)
    await verify_target_user(user_id)
    return await _execute(OrganizationFinanceStore(store).eligible(user_id, actor, q=q, limit=limit, offset=offset))


@router.get("/organizations/{organization_id}/consumption", response_model=OrganizationConsumption)
async def organization_consumption(
    request: Request, organization_id: str, user: Admin, store: Store,
    start: datetime | None = None, end: datetime | None = None,
    limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0, le=10000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    _queries(request, frozenset(("start", "end", "limit", "offset")))
    return await _execute(
        OrganizationFinanceStore(store).consumption(organization_id, actor, start=start, end=end, limit=limit, offset=offset)
    )
