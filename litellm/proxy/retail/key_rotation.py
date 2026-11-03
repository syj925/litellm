import asyncio
import hmac
from copy import copy
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final, Protocol

import psycopg
from fastapi import APIRouter, HTTPException
from prisma.errors import PrismaError
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from litellm.proxy._types import KeyManagementRoutes, LitellmUserRoles, RegenerateKeyRequest, UserAPIKeyAuth
from litellm.proxy.auth.auth_checks import delete_cache_key_object, get_jwt_key_mapping_cache_keys_for_token
from litellm.proxy.common_utils.auth_cache_invalidation_pubsub import evict_and_broadcast
from litellm.proxy.db.prisma_client import PrismaWrapper
from litellm.proxy.management_endpoints.key_management_endpoints import (
    TeamMemberPermissionChecks,
    _execute_virtual_key_regeneration,
    can_modify_verification_token,
)
from litellm.proxy.utils import PrismaClient, hash_token_if_needed
from litellm.repositories.verification_token_repository import VerificationTokenRepository

from .api import Store, User
from .control_plane_access import NativeIdentityReader, PrismaIdentityReader, native_master_actor, verify_actor
from .control_plane_http import PrivateManagementRoute

router: Final = APIRouter(prefix="/retail", tags=["Retail API keys"], route_class=PrivateManagementRoute)

if TYPE_CHECKING:
    from prisma import Prisma


class _RotationWriter(Protocol):
    async def query_raw(self, query: str, *args: object) -> object: ...


def writer_transaction_client(client: PrismaClient, transaction: "Prisma") -> PrismaClient:
    bound: Final = copy(client)
    bound.db = PrismaWrapper(original_prisma=transaction)
    return bound


async def lock_rotation_rows(writer: _RotationWriter, user: UserAPIKeyAuth, token: str) -> None:
    await writer.query_raw('SELECT user_id FROM "LiteLLM_UserTable" WHERE user_id=$1 FOR SHARE', user.user_id)
    await writer.query_raw('SELECT token FROM "LiteLLM_VerificationToken" WHERE token=$1 FOR SHARE', user.api_key)
    await writer.query_raw('SELECT token FROM "LiteLLM_VerificationToken" WHERE token=$1 FOR UPDATE', token)


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


async def rotation_user(
    user: UserAPIKeyAuth,
    master_key: str | None,
    *,
    reader: NativeIdentityReader | None = None,
    now: datetime | None = None,
) -> UserAPIKeyAuth:
    if native_master_actor(user, bool(master_key)):
        return user
    actor: Final = await verify_actor(user, reader=reader, now=now)
    if not actor.can_write:
        raise HTTPException(403, "Read-only accounts cannot rotate API keys")
    fresh_role: Final = (
        user.user_role
        if actor.native_role == LitellmUserRoles.PROXY_ADMIN.value and not actor.is_admin
        else LitellmUserRoles(actor.native_role)
    )
    return user.model_copy(update={"user_role": fresh_role})


@router.post("/keys/rotate", response_model=RotateKeyResponse)
async def rotate_key(data: RotateKeyRequest, user: User, store: Store) -> RotateKeyResponse:
    from litellm.proxy import proxy_server

    prisma: Final = proxy_server.prisma_client
    if prisma is None:
        raise HTTPException(503, "Key storage is temporarily unavailable")
    current: Final = await rotation_user(user, proxy_server.master_key)
    key: Final = data.key.get_secret_value()
    hashed: Final = rotation_target(key, proxy_server.master_key, current)
    try:
        async with store.diagnostic_transaction() as connection, asyncio.timeout(8):
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"retail:rotate:{hashed}",)
            )
            async with prisma.tx(timeout=timedelta(seconds=8)) as transaction:
                await transaction.execute_raw("SET LOCAL lock_timeout='1000ms'")
                await transaction.execute_raw("SET LOCAL statement_timeout='5000ms'")
                bound: Final = writer_transaction_client(prisma, transaction)
                writer: Final[_RotationWriter] = bound.writer_db
                await lock_rotation_rows(writer, current, hashed)
                verified: Final = await rotation_user(
                    user, proxy_server.master_key, reader=PrismaIdentityReader(bound.writer_db)
                )
                existing: Final = await VerificationTokenRepository(bound).find_by_id(hashed)
                if existing is None:
                    raise HTTPException(404, "API key does not exist or has already been rotated")
                if existing.team_id is not None:
                    await writer.query_raw(
                        'SELECT team_id FROM "LiteLLM_TeamTable" WHERE team_id=$1 FOR SHARE', existing.team_id
                    )
                await TeamMemberPermissionChecks.can_team_member_execute_key_management_endpoint(
                    user_api_key_dict=verified,
                    route=KeyManagementRoutes.KEY_REGENERATE,
                    prisma_client=bound,
                    existing_key_row=existing,
                    user_api_key_cache=proxy_server.user_api_key_cache,
                )
                if not await can_modify_verification_token(
                    key_info=existing,
                    user_api_key_cache=proxy_server.user_api_key_cache,
                    user_api_key_dict=verified,
                    prisma_client=bound,
                ):
                    raise HTTPException(403, "You are not authorized to rotate this API key")
                mappings: Final = await get_jwt_key_mapping_cache_keys_for_token(hashed, bound)
                rotated: Final = await _execute_virtual_key_regeneration(
                    prisma_client=bound,
                    llm_router=proxy_server.llm_router,
                    key_in_db=existing,
                    hashed_api_key=hashed,
                    key=key,
                    data=RegenerateKeyRequest(grace_period="0s"),
                    user_api_key_dict=verified,
                    litellm_changed_by=verified.user_id,
                    user_api_key_cache=proxy_server.user_api_key_cache,
                    proxy_logging_obj=proxy_server.proxy_logging_obj,
                )
            await delete_cache_key_object(
                hashed_token=hashed,
                user_api_key_cache=proxy_server.user_api_key_cache,
                proxy_logging_obj=proxy_server.proxy_logging_obj,
            )
            await evict_and_broadcast(cache_keys=mappings, user_api_key_cache=proxy_server.user_api_key_cache)
    except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests, PrismaError, TimeoutError) as exc:
        raise HTTPException(503, "Key storage is temporarily unavailable") from exc
    return RotateKeyResponse(
        key=rotated.key,
        token_id=rotated.token_id or hash_token_if_needed(rotated.key),
        key_name=rotated.key_name or "",
        rotated_at=datetime.now(timezone.utc),
    )
