from collections.abc import Mapping
from typing import Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import JsonValue

from litellm.proxy._types import UserAPIKeyAuth

from .api import Admin, OperationKey, User, _execute
from .budget_management import validate_user_id
from .control_plane_access import ControlPlaneActor, verify_actor, verify_target_user
from .control_plane_flags import monthly_budgets_enabled
from .control_plane_http import PrivateManagementRoute
from .control_plane_schema import ControlPlaneStore as Store
from .monthly_budget_store import CreateMonthlyBudget, MonthlyBudgetStore, StopMonthlyBudget, UpdateMonthlyBudget
from .organization_store import require_admin


def _enabled() -> None:
    if not monthly_budgets_enabled():
        raise HTTPException(404, "This feature is not enabled")


def monthly_query(request: Request, *, listing: bool) -> None:
    allowed: Final = frozenset(("q", "limit", "offset")) if listing else frozenset()
    if set(request.query_params) - allowed:
        raise HTTPException(422, "Monthly budget queries only accept model search and pagination on lists")


async def _admin_actor(user: UserAPIKeyAuth, user_id: str) -> ControlPlaneActor:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    validate_user_id(user_id)
    await verify_target_user(user_id)
    return actor


router: Final = APIRouter(
    prefix="/retail",
    tags=["Retail monthly model budgets"],
    route_class=PrivateManagementRoute,
    dependencies=[Depends(_enabled)],
)


@router.get("/admin/users/{user_id}/monthly-budgets")
async def admin_monthly_budgets(
    request: Request,
    user_id: str,
    user: Admin,
    store: Store,
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> Mapping[str, JsonValue]:
    actor: Final = await _admin_actor(user, user_id)
    monthly_query(request, listing=True)
    return await _execute(MonthlyBudgetStore(store).list(user_id, actor, q=q, limit=limit, offset=offset))


@router.post("/admin/users/{user_id}/monthly-budgets", status_code=201)
async def create_monthly_budget(
    request: Request,
    user_id: str,
    data: CreateMonthlyBudget,
    user: Admin,
    store: Store,
    key: OperationKey,
) -> Mapping[str, JsonValue]:
    actor: Final = await _admin_actor(user, user_id)
    monthly_query(request, listing=False)
    return await _execute(MonthlyBudgetStore(store).create(user_id, data, actor, key))


@router.get("/admin/users/{user_id}/monthly-budgets/{monthly_id}")
async def admin_monthly_budget(
    request: Request,
    user_id: str,
    monthly_id: UUID,
    user: Admin,
    store: Store,
) -> Mapping[str, JsonValue]:
    actor: Final = await _admin_actor(user, user_id)
    monthly_query(request, listing=False)
    return await _execute(MonthlyBudgetStore(store).get(user_id, monthly_id, actor))


@router.patch("/admin/users/{user_id}/monthly-budgets/{monthly_id}")
async def update_monthly_budget(
    request: Request,
    user_id: str,
    monthly_id: UUID,
    data: UpdateMonthlyBudget,
    user: Admin,
    store: Store,
    key: OperationKey,
) -> Mapping[str, JsonValue]:
    actor: Final = await _admin_actor(user, user_id)
    monthly_query(request, listing=False)
    return await _execute(MonthlyBudgetStore(store).update(user_id, monthly_id, data, actor, key))


@router.post("/admin/users/{user_id}/monthly-budgets/{monthly_id}/stop")
async def stop_monthly_budget(
    request: Request,
    user_id: str,
    monthly_id: UUID,
    data: StopMonthlyBudget,
    user: Admin,
    store: Store,
    key: OperationKey,
) -> Mapping[str, JsonValue]:
    actor: Final = await _admin_actor(user, user_id)
    monthly_query(request, listing=False)
    return await _execute(MonthlyBudgetStore(store).stop(user_id, monthly_id, data, actor, key))


@router.get("/monthly-budgets")
async def own_monthly_budgets(
    request: Request,
    user: User,
    store: Store,
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    monthly_query(request, listing=True)
    return await _execute(
        MonthlyBudgetStore(store).list(actor.user_id, actor, admin=False, q=q, limit=limit, offset=offset)
    )


@router.get("/monthly-budgets/{monthly_id}")
async def own_monthly_budget(request: Request, monthly_id: UUID, user: User, store: Store) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    monthly_query(request, listing=False)
    return await _execute(MonthlyBudgetStore(store).get(actor.user_id, monthly_id, actor, admin=False))
