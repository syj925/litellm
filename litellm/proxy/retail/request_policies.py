from collections.abc import Awaitable, Mapping
from typing import Annotated, Final, TypeAlias
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import JsonValue

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

from .api import Admin, OperationKey
from .control_plane_access import native_master_actor
from .control_plane_flags import request_policies_enabled
from .control_plane_http import PrivateManagementRoute
from .request_policy_management import (
    BindingCreate,
    BindingUpdate,
    PolicyActor,
    PolicyCreate,
    PolicyUpdate,
    PromptCreate,
    PromptUpdate,
    ResourceKind,
    TargetKind,
)
from .request_policy_service import get_request_policy_store
from .request_policy_store import NativeRequestPolicyStore, Reply
from .request_policy_types import RequestPolicyFailure

Store: TypeAlias = Annotated[NativeRequestPolicyStore, Depends(get_request_policy_store)]


def _enabled() -> None:
    if not request_policies_enabled():
        raise HTTPException(404, "This feature is not enabled")


def _actor(user: UserAPIKeyAuth, request: Request) -> PolicyActor:
    from litellm.proxy import proxy_server

    if user.user_role != LitellmUserRoles.PROXY_ADMIN or user.authenticated_by_custom_auth is not False:
        raise HTTPException(403, "A native proxy administrator credential is required")
    master: Final = native_master_actor(user, bool(proxy_server.master_key))
    return PolicyActor(user.user_id, None if master else user.token or user.api_key, master, request.url.path)


async def _execute(operation: Awaitable[Reply]) -> Mapping[str, JsonValue]:
    result: Final = await operation
    if isinstance(result, RequestPolicyFailure):
        match result.code:
            case "forbidden":
                raise HTTPException(403, "This administrator credential or target is no longer eligible")
            case "conflict":
                raise HTTPException(409, "The operation conflicts with the current version or assignment")
            case "not_found":
                raise HTTPException(404, "The requested policy record is unavailable")
            case "invalid":
                raise HTTPException(422, "The policy operation is invalid")
            case "capacity" | "unavailable":
                raise HTTPException(503, "Request policy authority is temporarily unavailable")
    return result


router: Final = APIRouter(prefix="/retail/admin/request-policies", tags=["Retail request policies"],
    route_class=PrivateManagementRoute, dependencies=[Depends(_enabled)])


@router.get("/targets")
async def targets(kind: TargetKind, user: Admin, request: Request, store: Store,
    q: str = Query(default="", max_length=100), limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000)) -> Mapping[str, JsonValue]:
    return await _execute(store.targets(_actor(user, request), kind, q=q, limit=limit, offset=offset))


@router.get("/{kind}")
async def list_resources(kind: ResourceKind, user: Admin, request: Request, store: Store,
    limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0, le=10000)) -> Mapping[str, JsonValue]:
    return await _execute(store.list_resources(_actor(user, request), kind, limit=limit, offset=offset))


@router.get("/{kind}/{identifier}")
async def get_resource(kind: ResourceKind, identifier: UUID, user: Admin, request: Request, store: Store) -> Mapping[str, JsonValue]:
    return await _execute(store.get_resource(_actor(user, request), kind, identifier))


@router.post("/prompts")
async def create_prompt(data: PromptCreate, user: Admin, request: Request, store: Store, key: OperationKey) -> Mapping[str, JsonValue]:
    return await _execute(store.write(_actor(user, request), "prompts", "created", key, data))


@router.post("/policies")
async def create_policy(data: PolicyCreate, user: Admin, request: Request, store: Store, key: OperationKey) -> Mapping[str, JsonValue]:
    return await _execute(store.write(_actor(user, request), "policies", "created", key, data))


@router.post("/bindings")
async def create_binding(data: BindingCreate, user: Admin, request: Request, store: Store, key: OperationKey) -> Mapping[str, JsonValue]:
    return await _execute(store.write(_actor(user, request), "bindings", "created", key, data))


@router.put("/prompts/{identifier}")
async def update_prompt(identifier: UUID, data: PromptUpdate, user: Admin, request: Request, store: Store, key: OperationKey) -> Mapping[str, JsonValue]:
    return await _execute(store.write(_actor(user, request), "prompts", "updated", key, data, identifier, data.expected_version))


@router.put("/policies/{identifier}")
async def update_policy(identifier: UUID, data: PolicyUpdate, user: Admin, request: Request, store: Store, key: OperationKey) -> Mapping[str, JsonValue]:
    return await _execute(store.write(_actor(user, request), "policies", "updated", key, data, identifier, data.expected_version))


@router.put("/bindings/{identifier}")
async def update_binding(identifier: UUID, data: BindingUpdate, user: Admin, request: Request, store: Store, key: OperationKey) -> Mapping[str, JsonValue]:
    return await _execute(store.write(_actor(user, request), "bindings", "updated", key, data, identifier, data.expected_version))


@router.delete("/{kind}/{identifier}")
async def delete_resource(kind: ResourceKind, identifier: UUID, user: Admin, request: Request, store: Store,
    key: OperationKey, version: int = Query(ge=1, le=9007199254740991)) -> Mapping[str, JsonValue]:
    return await _execute(store.write(_actor(user, request), kind, "deleted", key, None, identifier, version))
