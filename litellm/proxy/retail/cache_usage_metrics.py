import asyncio
from datetime import datetime, timedelta, timezone
from typing import Annotated, Final, Literal, TypeAlias

import psycopg
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

from .api import Store, User
from .cache_observations import TTL_AGGREGATE, legacy_cache_sql
from .channel_metrics import _HELPER, _READ, _WRITE
from .customer_access import is_retail_customer
from .store import WalletStore, _many

router: Final = APIRouter(tags=["Cache usage"])
_Filter: Final = Field(default=None, min_length=1, max_length=191)
_ModelName: TypeAlias = Annotated[str, Field(min_length=1, max_length=191)]


class CacheUsageQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_date: datetime
    end_date: datetime
    scope: Literal["global", "self"] = "self"
    user_id: str | None = _Filter
    api_key: str | None = _Filter
    team_id: str | None = _Filter
    model: str | None = _Filter
    model_group: str | None = _Filter
    models: tuple[_ModelName, ...] = Field(default=(), max_length=500)
    model_groups: tuple[_ModelName, ...] = Field(default=(), max_length=500)


class CacheUsageMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cache_write_ttl_tokens: dict[str, int] = Field(default_factory=dict)
    cache_write_unclassified_tokens: int | None = Field(default=None, ge=0)
    request_count: int = Field(default=0, ge=0)
    cache_usage_samples: int = Field(default=0, ge=0)
    cache_usage_unknown_requests: int = Field(default=0, ge=0)


def cache_usage_window(query: CacheUsageQuery) -> tuple[datetime, datetime]:
    if query.start_date.tzinfo is None or query.end_date.tzinfo is None:
        raise HTTPException(status_code=422, detail="Cache usage dates must include a UTC offset")
    start: Final = query.start_date.astimezone(timezone.utc).replace(tzinfo=None)
    end: Final = query.end_date.astimezone(timezone.utc).replace(tzinfo=None)
    if not timedelta(0) < end - start <= timedelta(days=31):
        raise HTTPException(status_code=422, detail="Cache usage requires a range of at most 31 days")
    return start, end


def cache_usage_owner(query: CacheUsageQuery, user: UserAPIKeyAuth) -> str | None:
    admin: Final = user.user_role in {LitellmUserRoles.PROXY_ADMIN, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY}
    if query.scope == "global":
        if not admin:
            raise HTTPException(status_code=403, detail="Global cache usage requires proxy administrator permission")
        return query.user_id
    if not user.user_id or query.user_id not in (None, user.user_id):
        raise HTTPException(status_code=403, detail="Personal cache usage is limited to the authenticated user")
    return user.user_id


_MODEL: Final = "CASE WHEN %s::boolean THEN COALESCE(NULLIF(s.model_group,''),s.model) ELSE s.model END"
_QUERY: Final = f"""WITH observed AS (
 SELECT COALESCE(co.cache_read_tokens,{_READ}) AS cache_read_tokens,
 COALESCE(co.cache_write_tokens,legacy_cache.cache_write_tokens) AS cache_write_tokens,
 COALESCE(co.cache_write_ttl_tokens,legacy_cache.cache_write_ttl_tokens) AS cache_write_ttl_tokens,
 COALESCE(co.cache_write_unclassified_tokens,legacy_cache.cache_write_unclassified_tokens)
      AS cache_write_unclassified_tokens
 FROM "LiteLLM_SpendLogs" s
 LEFT JOIN retail_cache_observations co ON co.litellm_call_id=s.litellm_call_id
 AND co.user_id=COALESCE(s."user",'') AND co.started_at=date_trunc('milliseconds',s."startTime")
 {legacy_cache_sql(_WRITE)}
 WHERE s."startTime">=%s AND s."startTime"<%s AND NOT ({_HELPER})
 AND (%s::text IS NULL OR s."user"=%s)
 AND (%s::text IS NULL OR s.api_key=%s)
 AND (%s::text IS NULL OR s.team_id=%s)
 AND (%s::text IS NULL OR ({_MODEL})=%s)
 AND (%s::text IS NULL OR COALESCE(NULLIF(s.model_group,''),s.model)=%s)
 AND (%s::text[] IS NULL OR ({_MODEL})=ANY(%s::text[]))
 AND (%s::text[] IS NULL OR COALESCE(NULLIF(s.model_group,''),s.model)=ANY(%s::text[]))
)
SELECT sum(cache_read_tokens)::bigint AS cache_read_tokens,
 sum(cache_write_tokens)::bigint AS cache_write_tokens,
 sum(cache_write_unclassified_tokens)::bigint AS cache_write_unclassified_tokens,
 {TTL_AGGREGATE}, count(*) AS request_count,
 count(*) FILTER(WHERE cache_read_tokens IS NOT NULL AND cache_write_tokens IS NOT NULL) AS cache_usage_samples,
 count(*) FILTER(WHERE cache_read_tokens IS NULL OR cache_write_tokens IS NULL) AS cache_usage_unknown_requests
FROM observed"""


async def load_cache_usage_metrics(
    store: WalletStore, query: CacheUsageQuery, owner: str | None,
    *, public_models: bool = False,
) -> CacheUsageMetrics:
    start, end = cache_usage_window(query)
    models: Final = list(query.models) if query.models else None
    groups: Final = list(query.model_groups) if query.model_groups else None
    async with store.transaction() as connection:
        await connection.execute("SET TRANSACTION READ ONLY")
        await connection.execute("SET LOCAL statement_timeout = '3000ms'")
        rows: Final = await _many(
            connection, _QUERY,
            (start, end, owner, owner, query.api_key, query.api_key, query.team_id, query.team_id,
             query.model, public_models, query.model, query.model_group, query.model_group,
             models, public_models, models, groups, groups),
            CacheUsageMetrics,
        )
    return rows[0] if rows else CacheUsageMetrics()


@router.get("/cache-usage", response_model=CacheUsageMetrics)
async def cache_usage_metrics(
    query: Annotated[CacheUsageQuery, Query()], user: User, store: Store, response: Response,
) -> CacheUsageMetrics:
    owner: Final = cache_usage_owner(query, user)
    cache_usage_window(query)
    response.headers["Cache-Control"] = "no-store"
    try:
        return await asyncio.wait_for(
            load_cache_usage_metrics(store, query, owner, public_models=is_retail_customer(user)), timeout=3,
        )
    except (TimeoutError, psycopg.Error) as exc:
        raise HTTPException(status_code=503, detail="Cache usage is temporarily unavailable for this filter") from exc
