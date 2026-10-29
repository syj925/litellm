import hmac
from datetime import datetime, timezone
from typing import Final

import psycopg
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from litellm.proxy._types import KeyManagementRoutes, LitellmUserRoles, RegenerateKeyRequest, UserAPIKeyAuth
from litellm.proxy.management_endpoints.key_management_endpoints import (
    TeamMemberPermissionChecks,
    _execute_virtual_key_regeneration,
    can_modify_verification_token,
)
from litellm.proxy.utils import hash_token_if_needed
from litellm.repositories.verification_token_repository import VerificationTokenRepository

from .api import Store, User

router: Final = APIRouter(prefix="/retail", tags=["Retail API keys"])


class RotateKeyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: SecretStr = Field(min_length=1, max_length=512)


class RotateKeyResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str = Field(repr=False)
    token_id: str
    key_name: str
    rotated_at: datetime


def rotation_target(key: str, master_key: str | None, user: UserAPIKeyAuth) -> str:
    if user.user_role in (LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY, LitellmUserRoles.INTERNAL_USER_VIEW_ONLY):
        raise HTTPException(403, "Read-only accounts cannot rotate API keys")
    hashed: Final = hash_token_if_needed(key)
    if master_key is not None and hmac.compare_digest(hashed, hash_token_if_needed(master_key)):
        raise HTTPException(400, "Master key rotation is not available through this endpoint")
    return hashed


@router.post("/keys/rotate", response_model=RotateKeyResponse)
async def rotate_key(data: RotateKeyRequest, user: User, store: Store, response: Response) -> RotateKeyResponse:
    from litellm.proxy import proxy_server

    prisma: Final = proxy_server.prisma_client
    if prisma is None:
        raise HTTPException(503, "Key storage is temporarily unavailable")
    key: Final = data.key.get_secret_value()
    hashed: Final = rotation_target(key, proxy_server.master_key, user)
    async with await psycopg.AsyncConnection.connect(store.database_url, connect_timeout=10) as connection:
        await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"retail:rotate:{hashed}",))
        existing: Final = await VerificationTokenRepository(prisma).find_by_id(hashed)
        if existing is None:
            raise HTTPException(404, "API key does not exist or has already been rotated")
        await TeamMemberPermissionChecks.can_team_member_execute_key_management_endpoint(
            user_api_key_dict=user,
            route=KeyManagementRoutes.KEY_REGENERATE,
            prisma_client=prisma,
            existing_key_row=existing,
            user_api_key_cache=proxy_server.user_api_key_cache,
        )
        if not await can_modify_verification_token(
            key_info=existing,
            user_api_key_cache=proxy_server.user_api_key_cache,
            user_api_key_dict=user,
            prisma_client=prisma,
        ):
            raise HTTPException(403, "You are not authorized to rotate this API key")
        rotated: Final = await _execute_virtual_key_regeneration(
            prisma_client=prisma,
            llm_router=proxy_server.llm_router,
            key_in_db=existing,
            hashed_api_key=hashed,
            key=key,
            data=RegenerateKeyRequest(grace_period="0s"),
            user_api_key_dict=user,
            litellm_changed_by=user.user_id,
            user_api_key_cache=proxy_server.user_api_key_cache,
            proxy_logging_obj=proxy_server.proxy_logging_obj,
        )
    response.headers["Cache-Control"] = "no-store"
    return RotateKeyResponse(
        key=rotated.key,
        token_id=rotated.token_id or hash_token_if_needed(rotated.key),
        key_name=rotated.key_name or "",
        rotated_at=datetime.now(timezone.utc),
    )
