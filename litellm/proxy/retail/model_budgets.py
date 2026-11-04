from collections.abc import Mapping
from typing import Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import JsonValue

from .api import Admin, OperationKey, User, _execute
from .budget_management import (
    BudgetManagementStore,
    CreateModelBudget,
    ScheduleModelBudget,
    UpdateModelBudget,
    validate_user_id,
)
from .control_plane_access import verify_actor, verify_target_user
from .control_plane_flags import model_budgets_enabled
from .control_plane_http import PrivateManagementRoute
from .control_plane_schema import ControlPlaneStore as Store
from .organization_store import require_admin


def _enabled() -> None:
    if not model_budgets_enabled():
        raise HTTPException(404, "This feature is not enabled")


router: Final = APIRouter(
    prefix="/retail",
    tags=["Retail model budgets"],
    route_class=PrivateManagementRoute,
    dependencies=[Depends(_enabled)],
)


@router.get("/admin/users/{user_id}/model-budgets/models")
async def budget_models(
    user_id: str,
    user: Admin,
    store: Store,
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    validate_user_id(user_id)
    await verify_target_user(user_id)
    return await _execute(BudgetManagementStore(store).models(user_id, actor, q=q, limit=limit, offset=offset))


@router.get("/admin/users/{user_id}/model-budgets")
async def admin_budgets(
    user_id: str,
    user: Admin,
    store: Store,
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    validate_user_id(user_id)
    await verify_target_user(user_id)
    return await _execute(
        BudgetManagementStore(store).list(user_id, actor, admin=True, q=q, limit=limit, offset=offset)
    )


@router.post("/admin/users/{user_id}/model-budgets", status_code=201)
async def create_budget(
    user_id: str, data: CreateModelBudget, user: Admin, store: Store, key: OperationKey
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    validate_user_id(user_id)
    await verify_target_user(user_id)
    return await _execute(BudgetManagementStore(store).create(user_id, data, actor, key))


@router.patch("/admin/users/{user_id}/model-budgets/{budget_id}")
async def update_budget(
    user_id: str, budget_id: UUID, data: UpdateModelBudget, user: Admin, store: Store, key: OperationKey
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    validate_user_id(user_id)
    await verify_target_user(user_id)
    return await _execute(BudgetManagementStore(store).update(user_id, budget_id, data, actor, key))


@router.get("/model-budgets")
async def own_budgets(
    request: Request,
    user: User,
    store: Store,
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    if set(request.query_params) - {"q", "limit", "offset"}:
        raise HTTPException(422, "Personal budget queries only accept model search and pagination")
    return await _execute(
        BudgetManagementStore(store).list(actor.user_id, actor, admin=False, q=q, limit=limit, offset=offset)
    )


@router.post("/admin/users/{user_id}/model-budgets/{budget_id}/successor", status_code=201)
async def schedule_budget(
    user_id: str, budget_id: UUID, data: ScheduleModelBudget, user: Admin, store: Store, key: OperationKey
) -> Mapping[str, JsonValue]:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    validate_user_id(user_id)
    await verify_target_user(user_id)
    return await _execute(BudgetManagementStore(store).schedule(user_id, budget_id, data, actor, key))
