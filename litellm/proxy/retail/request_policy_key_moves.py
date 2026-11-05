import os
from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import HTTPException

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

from .control_plane_access import native_master_actor
from .request_policy_management import PolicyActor
from .request_policy_types import RequestPolicyFailure


async def _verify_administrator(actor: PolicyActor) -> RequestPolicyFailure | None:
    from .request_policy_service import get_request_policy_store

    return await get_request_policy_store().verify_administrator(actor)


async def guard_key_team_change(
    auth: UserAPIKeyAuth | None, previous_team_id: str | None, requested_team_id: str | None,
    *, provided: bool,
    verify: Callable[[PolicyActor], Awaitable[RequestPolicyFailure | None]] = _verify_administrator,
) -> None:
    if os.environ.get("RETAIL_REQUEST_POLICIES_ENABLED", "false").lower() != "true":
        return
    if not provided or previous_team_id == requested_team_id:
        return
    if auth is None or auth.authenticated_by_custom_auth is not False or auth.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(403, "Changing a key's team requires a current gateway administrator")
    from litellm.proxy import proxy_server

    master: Final = native_master_actor(auth, bool(proxy_server.master_key))
    actor: Final = PolicyActor(auth.user_id, None if master else auth.token or auth.api_key,
        master, auth.request_route or "/key/update")
    result: Final = await verify(actor)
    if result is None:
        return
    if result.code in {"unavailable", "capacity"}:
        raise HTTPException(503, "Request policy authority is temporarily unavailable")
    raise HTTPException(403, "Changing a key's team requires a current gateway administrator")
