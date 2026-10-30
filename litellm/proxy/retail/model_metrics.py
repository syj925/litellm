import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from hashlib import sha256
from time import monotonic
from typing import Final, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from .api import Store, User
from .catalog import _listed_request_name, _request_name
from .channel_metrics import _HELPER
from .pool_store import (
    PoolAccess,
    PoolStore,
    _live_bindings,
    _PoolRow,
    _StoredBinding,
    effective_account_grants,
    router_deployments,
)
from .store import WalletStore, _many


class ModelMetricHours(IntEnum):
    DAY = 24
    WEEK = 168
    MONTH = 720


class ModelMetricsWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    hours: ModelMetricHours
    bucket_minutes: int


class ModelMetricSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    unknown_status_requests: int = 0
    success_rate_pct: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    output_tokens_per_second_p50: float | None = None
    latency_samples: int = 0
    ttft_samples: int = 0
    throughput_samples: int = 0
    usage_samples: int = 0


class ModelMetricsTrend(ModelMetricSummary):
    time: datetime


class ModelMetricsPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    scope: Literal["gateway", "account"]
    window: ModelMetricsWindow
    summary: ModelMetricSummary
    trend: tuple[ModelMetricsTrend, ...]
    notes: tuple[str, ...]


class _Aggregate(ModelMetricSummary):
    is_summary: bool
    bucket: datetime | None = None


_NOTES: Final = (
    "These are observations from this gateway, not vendor benchmarks or an availability guarantee.",
    "Only final request logs with exact public model names and currently eligible deployment IDs are included. "
    "Billed retry helper rows, missing deployment IDs and pre-dispatch rejections are excluded.",
    "Public statistics use enabled public resources. Personal statistics use your own requests and currently "
    "authorized resources. Changes to resource visibility or model access can change historical coverage.",
    "Success rate is unavailable when any included request has an unknown status. Token totals describe final "
    "request usage, excluding retry helper rows; they are not billed tokens or wallet charges.",
    "TTFT and output speed require a correlated server observation of actual model text, reasoning or tool "
    "arguments in a completed successful stream. Native first-event timestamps alone are not TTFT samples. "
    "Response-cache hits and unverified historical streams are excluded. Observed output speed is final output "
    "tokens divided by the time from the first model output to completion of stream delivery at the gateway; "
    "it can include client backpressure and is not an instantaneous vendor generation speed.",
    "Windows use request start time and UTC buckets. The first and last buckets may be partial. Empty buckets "
    "have zero requests and unknown rates or timings. Asynchronous logging can delay recent observations. "
    "Aggregates may be cached for 20 seconds; resource access is checked on every request.",
)


def model_metrics_window(hours: ModelMetricHours, now: datetime) -> ModelMetricsWindow:
    return ModelMetricsWindow(
        start=now - timedelta(hours=hours), end=now, hours=hours,
        bucket_minutes=15 if hours == ModelMetricHours.DAY else 60,
    )


def selected_deployment_ids(
    model: str,
    pools: Sequence[_PoolRow],
    bindings: Sequence[_StoredBinding],
    deployments: Sequence[Mapping[str, object]],
    access: PoolAccess | None,
) -> tuple[str, ...]:
    live: Final = _live_bindings(bindings, deployments)
    if access is None:
        names: Final = frozenset(_request_name(item) for item in deployments if _listed_request_name(item))
        if model not in names:
            raise HTTPException(404, "Model is not available")
        eligible_pools: Final = frozenset(pool.id for pool in pools if pool.enabled and pool.visibility == "public")
        return tuple(sorted({item.deployment_id for item in live if item.model == model and item.pool_id in eligible_pools}))
    grants: Final = effective_account_grants(access, pools, live)
    allowed_pools: Final = frozenset(grant.pool_id for grant in grants if model in grant.models)
    if not allowed_pools:
        raise HTTPException(404, "Model is not available to this account")
    return tuple(sorted({item.deployment_id for item in live if item.model == model and item.pool_id in allowed_pools}))


def summarize(row: _Aggregate | None) -> ModelMetricSummary:
    if row is None:
        return ModelMetricSummary()
    rate: Final = (
        100 * row.successful_requests / row.requests
        if row.requests and row.unknown_status_requests == 0 else None
    )
    return ModelMetricSummary.model_validate(
        {**row.model_dump(exclude={"is_summary", "bucket"}), "success_rate_pct": rate}
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _bucket_times(window: ModelMetricsWindow) -> tuple[datetime, ...]:
    seconds: Final = window.bucket_minutes * 60
    start: Final = int(window.start.timestamp()) // seconds * seconds
    end: Final = int(window.end.timestamp()) // seconds * seconds
    return tuple(datetime.fromtimestamp(value, timezone.utc) for value in range(start, end + 1, seconds))


def metrics_page(
    model: str, scope: Literal["gateway", "account"], window: ModelMetricsWindow, rows: Sequence[_Aggregate]
) -> ModelMetricsPage:
    summary: Final = next((item for item in rows if item.is_summary), None)
    buckets: Final = {_utc(item.bucket): item for item in rows if not item.is_summary and item.bucket is not None}
    return ModelMetricsPage(
        model=model, scope=scope, window=window, summary=summarize(summary),
        trend=tuple(
            ModelMetricsTrend(time=time, **summarize(buckets.get(time)).model_dump()) for time in _bucket_times(window)
        ),
        notes=_NOTES,
    )


_QUERY: Final = f"""WITH observed AS (
 SELECT s.\"startTime\" AS started,lower(s.status) AS status,
 CASE WHEN s.prompt_tokens>=0 THEN s.prompt_tokens END AS input_tokens,
 CASE WHEN s.completion_tokens>=0 THEN s.completion_tokens END AS output_tokens,
 CASE WHEN s.total_tokens>=0 THEN s.total_tokens END AS total_tokens,
 CASE WHEN jsonb_typeof(s.metadata->'observed_stream')='boolean'
      THEN (s.metadata->>'observed_stream')::boolean END AS stream,
 CASE WHEN lower(s.cache_hit) IN ('true','false') THEN lower(s.cache_hit)='true' END AS cache_hit,
 CASE WHEN s.request_duration_ms>=0 AND s.request_duration_ms::text NOT IN ('NaN','Infinity','-Infinity')
      THEN s.request_duration_ms::double precision
      WHEN isfinite(s.\"endTime\") AND s.\"endTime\">=s.\"startTime\"
      THEN extract(epoch FROM (s.\"endTime\"-s.\"startTime\"))*1000 END AS latency,
 CASE WHEN o.first_output_at>=s.\"startTime\" AND o.first_output_at<=s.\"endTime\"
      AND o.ended_at>=o.first_output_at AND isfinite(o.first_output_at)
      AND isfinite(o.ended_at) AND isfinite(s.\"endTime\")
      THEN extract(epoch FROM (o.first_output_at-s.\"startTime\"))*1000 END AS ttft,
 CASE WHEN o.first_output_at>=s.\"startTime\" AND o.first_output_at<=s.\"endTime\"
      AND o.ended_at>o.first_output_at AND isfinite(o.first_output_at)
      AND isfinite(o.ended_at) AND isfinite(s.\"endTime\") AND s.completion_tokens>0
      THEN s.completion_tokens / extract(epoch FROM (o.ended_at-o.first_output_at)) END AS output_speed
 FROM \"LiteLLM_SpendLogs\" s
 LEFT JOIN retail_model_observations o ON o.litellm_call_id=s.litellm_call_id
      AND o.user_id=COALESCE(s.\"user\",'') AND o.started_at=date_trunc('milliseconds',s.\"startTime\")
 WHERE s.\"startTime\">=%s AND s.\"startTime\"<%s
 AND COALESCE(NULLIF(s.model_group,''),NULLIF(s.model,''),'')=%s
 AND (%s::text IS NULL OR s.\"user\"=%s)
 AND s.model_id=ANY(%s::text[]) AND NOT ({_HELPER})
), samples AS (
 SELECT *,to_timestamp(floor(extract(epoch FROM started)/(%s*60))*(%s*60)) AS bucket,
 CASE WHEN stream IS TRUE AND status='success' AND cache_hit IS NOT TRUE THEN ttft END AS stream_ttft,
 CASE WHEN stream IS TRUE AND status='success' AND cache_hit IS NOT TRUE THEN output_speed END AS stream_speed
 FROM observed
)
SELECT grouping(bucket)=1 AS is_summary,bucket,
 count(*) AS requests,
 count(*) FILTER(WHERE status='success') AS successful_requests,
 count(*) FILTER(WHERE status='failure') AS failed_requests,
 count(*) FILTER(WHERE status IS NULL OR status NOT IN ('success','failure')) AS unknown_status_requests,
 sum(input_tokens)::bigint AS input_tokens,sum(output_tokens)::bigint AS output_tokens,
 sum(total_tokens)::bigint AS total_tokens,count(total_tokens) AS usage_samples,
 percentile_cont(0.5) WITHIN GROUP(ORDER BY latency)::double precision AS latency_p50_ms,
 percentile_cont(0.95) WITHIN GROUP(ORDER BY latency)::double precision AS latency_p95_ms,
 percentile_cont(0.5) WITHIN GROUP(ORDER BY stream_ttft)::double precision AS ttft_p50_ms,
 percentile_cont(0.95) WITHIN GROUP(ORDER BY stream_ttft)::double precision AS ttft_p95_ms,
 percentile_cont(0.5) WITHIN GROUP(ORDER BY stream_speed)::double precision AS output_tokens_per_second_p50,
 count(latency) AS latency_samples,count(stream_ttft) AS ttft_samples,count(stream_speed) AS throughput_samples
FROM samples GROUP BY GROUPING SETS((),(bucket))"""


async def load_model_metrics(
    store: WalletStore, model: str, hours: ModelMetricHours, user_id: str | None, now: datetime,
    *, deployments: Sequence[Mapping[str, object]] | None = None,
) -> ModelMetricsPage:
    selected: Final = await _selection(store, model, user_id, deployments)
    return await _load_selected(store, model, hours, user_id, now, selected)


async def _selection(
    store: WalletStore, model: str, user_id: str | None, deployments: Sequence[Mapping[str, object]] | None = None
) -> tuple[str, ...]:
    pool_store: Final = PoolStore(store)
    current_deployments: Final = router_deployments() if deployments is None else deployments
    async with store.transaction() as connection:
        await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        await connection.execute("SET LOCAL statement_timeout = '8000ms'")
        pools, bindings = await pool_store._state(connection)
        access: Final = await pool_store._access(connection, user_id) if user_id is not None else None
        return selected_deployment_ids(model, pools, bindings, current_deployments, access)


async def _load_selected(
    store: WalletStore, model: str, hours: ModelMetricHours, user_id: str | None,
    now: datetime, selected: tuple[str, ...],
) -> ModelMetricsPage:
    window: Final = model_metrics_window(hours, now)
    if not selected:
        return metrics_page(model, "account" if user_id is not None else "gateway", window, ())
    async with store.transaction() as connection:
        await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        await connection.execute("SET LOCAL statement_timeout = '8000ms'")
        rows: Final = await _many(
            connection, _QUERY,
            (
                window.start.astimezone(timezone.utc).replace(tzinfo=None),
                window.end.astimezone(timezone.utc).replace(tzinfo=None),
                model,user_id,user_id,list(selected),window.bucket_minutes,window.bucket_minutes,
            ),
            _Aggregate,
        )
    return metrics_page(model, "account" if user_id is not None else "gateway", window, rows)


_CacheKey = tuple[str, str | None, str, ModelMetricHours, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _CachedMetrics:
    expires: float
    page: ModelMetricsPage


class _MetricsCache:
    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._loading = asyncio.Semaphore(2)
        self._entries: dict[_CacheKey, _CachedMetrics] = {}

    async def _cached(self, key: _CacheKey) -> ModelMetricsPage | None:
        async with self._lock:
            cached: Final = self._entries.get(key)
            return cached.page if cached is not None and cached.expires > self._clock() else None

    async def get(self, key: _CacheKey, loader: Callable[[], Awaitable[ModelMetricsPage]]) -> ModelMetricsPage:
        cached: Final = await self._cached(key)
        if cached is not None:
            return cached
        async with self._loading:
            previous: Final = await self._cached(key)
            if previous is not None:
                return previous
            page: Final = await loader()
            async with self._lock:
                now: Final = self._clock()
                alive: Final = {item: value for item, value in self._entries.items() if value.expires > now}
                self._entries = {**dict(tuple(alive.items())[-127:]), key: _CachedMetrics(now + 20, page)}
            return page


_CACHE: Final = _MetricsCache()


async def _cached_page(store: WalletStore, model: str, hours: ModelMetricHours, user_id: str | None) -> ModelMetricsPage:
    selected: Final = await _selection(store, model, user_id)
    key: Final = (sha256(store.database_url.encode()).hexdigest(), user_id, model, hours, selected)
    return await _CACHE.get(
        key, lambda: _load_selected(store, model, hours, user_id, datetime.now(timezone.utc), selected)
    )


async def _page(store: WalletStore, model: str, hours: ModelMetricHours, user_id: str | None) -> ModelMetricsPage:
    try:
        return await asyncio.wait_for(_cached_page(store, model, hours, user_id), 10)
    except (psycopg.Error, asyncio.TimeoutError) as exc:
        raise HTTPException(503, "Model performance data is temporarily unavailable") from exc


router: Final = APIRouter(tags=["Model performance"])


@router.get("/public/model-metrics", response_model=ModelMetricsPage)
async def public_model_metrics(
    store: Store, response: Response, model: str = Query(min_length=1, max_length=191),
    hours: ModelMetricHours = ModelMetricHours.DAY,
) -> ModelMetricsPage:
    response.headers["Cache-Control"] = "no-store"
    return await _page(store, model, hours, None)


@router.get("/model-metrics", response_model=ModelMetricsPage)
async def personal_model_metrics(
    user: User, store: Store, response: Response, model: str = Query(min_length=1, max_length=191),
    hours: ModelMetricHours = ModelMetricHours.DAY,
) -> ModelMetricsPage:
    response.headers["Cache-Control"] = "no-store"
    return await _page(store, model, hours, user.user_id or "")
