import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from hashlib import sha256
from time import monotonic
from typing import Final, Literal

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .api import Admin, Store
from .cache_observations import TTL_AGGREGATE, legacy_cache_sql
from .cache_usage import CACHE_DETAIL_FIELDS, CACHE_READ_FIELDS, CACHE_WRITE_FIELDS
from .store import WalletStore, _many


class Hours(IntEnum):
    HOUR = 1
    SIX_HOURS = 6
    DAY = 24
    WEEK = 168
    MONTH = 720


router: Final = APIRouter(tags=["Channel activity"])


class MetricsWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    hours: Hours
    start: datetime
    end: datetime
    bucket_minutes: int
    rolling_seconds: Literal[60] = 60


class PeriodMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    unknown_status_requests: int = 0
    error_rate_pct: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_write_ttl_tokens: dict[str, int] = Field(default_factory=dict)
    cache_write_unclassified_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_hit_requests: int = 0
    cache_hit_rate_pct: float | None = None
    cache_status_samples: int = 0
    streaming_requests: int = 0
    billed_attempts: int = 0
    usage_samples: int = 0
    spend_samples: int = 0
    latency_samples: int = 0
    ttft_samples: int = 0
    native_spend_usd: str | None = None
    latency_avg_ms: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    ttft_avg_ms: float | None = None
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None


class RollingMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_seconds: Literal[60] = 60
    requests: int = 0
    rpm: float | None = None
    tpm: float | None = None
    native_usd_per_minute: str | None = None


class MetricsCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: Literal["gateway"] = "gateway"
    window_log_rows: int
    attributed_requests: int
    unattributed_requests: int
    model_id_coverage: float | None


class ChannelIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    pool_id: str
    pool_name: str
    status: Literal["active", "provisioning", "error"]
    deployment_count: int


class ChannelActivity(ChannelIdentity):
    metrics: PeriodMetrics
    rolling: RollingMetrics
    upstream_error_events: int


class ModelActivity(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    metrics: PeriodMetrics
    rolling: RollingMetrics


class MetricsTrend(BaseModel):
    model_config = ConfigDict(frozen=True)

    time: datetime
    metrics: PeriodMetrics


class MetricsErrorGroup(BaseModel):
    model_config = ConfigDict(frozen=True)

    error_type: str
    status_code: int | None
    count: int


class SafeMetricsError(BaseModel):
    model_config = ConfigDict(frozen=True)

    time: datetime
    source: Literal["request", "upstream_event"]
    request_id: str
    model: str
    error_type: str
    status_code: int | None
    message: str


class ChannelMetricsPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    window: MetricsWindow
    channels: tuple[ChannelActivity, ...]
    coverage: MetricsCoverage
    notes: tuple[str, ...]


class ChannelMetricsDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    window: MetricsWindow
    channel: ChannelIdentity
    metrics: PeriodMetrics
    rolling: RollingMetrics
    upstream_error_events: int
    models: tuple[ModelActivity, ...]
    trend: tuple[MetricsTrend, ...]
    request_error_groups: tuple[MetricsErrorGroup, ...]
    upstream_error_groups: tuple[MetricsErrorGroup, ...]
    recent_errors: tuple[SafeMetricsError, ...]
    coverage: MetricsCoverage
    notes: tuple[str, ...]


class _Aggregate(PeriodMetrics):
    channel_id: str
    kind: Literal["channel", "model", "trend"] = "channel"
    model: str | None = None
    bucket: datetime | None = None


class _CoverageRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_log_rows: int
    final_requests: int
    attributed_requests: int
    identified_rows: int


class _ErrorRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_id: str
    source: Literal["request", "upstream_event"]
    error_type: str
    status_code: str | None
    count: int = 0
    time: datetime | None = None
    request_id: str = ""
    model: str = ""


_NOTES: Final = (
    "Requests count final gateway request logs; billed retry helper rows are excluded from request and error rates.",
    "Native spend and token totals include recorded billed attempts. Native spend is not a customer wallet charge, "
    "supplier invoice, or profit; missing native logs and unrecorded retries are not estimated.",
    "Windows use request start time. Rolling 60s includes requests started in that interval and already logged; "
    "long requests and asynchronous log delivery can delay observations. These are not live in-flight counts or limits.",
    "Attribution uses exact deployment IDs and current channel membership. Historical moves can reassign old logs; "
    "missing IDs, pre-dispatch rejections, and removed deployments remain unattributed.",
    "Latency is gateway request duration. TTFT uses successful, explicitly observed streaming requests with valid "
    "timestamps and excludes response-cache hits; historical unknown streaming flags produce no TTFT sample.",
    "Cache hits mean gateway response-cache hits, not provider prompt-cache reads. Upstream error events are "
    "separate from final request failures and may include retries. Recent errors contain generated safe summaries only.",
    "Empty trend buckets have no logged observations. Provisioning status is channel configuration state, not health. "
    "Results may be cached for 8 seconds; no active probes are run.",
    "Cache usage describes final request logs, excluding retry helper rows. Write totals include each token once. "
    "TTL categories are subsets of that total; historical writes without a duration remain unspecified.",
)


def metrics_window(hours: Hours, now: datetime) -> MetricsWindow:
    bucket: Final = {1: 1, 6: 5, 24: 15, 168: 60, 720: 60}[hours]
    return MetricsWindow(hours=hours, start=now - timedelta(hours=hours), end=now, bucket_minutes=bucket)


def period_metrics(row: _Aggregate | None) -> PeriodMetrics:
    if row is None:
        return PeriodMetrics()
    error_rate: Final = (
        100 * row.failed_requests / row.requests if row.requests and row.unknown_status_requests == 0 else None
    )
    cache_rate: Final = (
        100 * row.cache_hit_requests / row.requests
        if row.requests and row.cache_status_samples == row.requests
        else None
    )
    return PeriodMetrics.model_validate(
        {
            **row.model_dump(exclude={"channel_id", "kind", "model", "bucket"}),
            "error_rate_pct": error_rate,
            "cache_hit_rate_pct": cache_rate,
        }
    )


def rolling_metrics(row: _Aggregate | None) -> RollingMetrics:
    if row is None:
        return RollingMetrics(rpm=0.0)
    return RollingMetrics(
        requests=row.requests,
        rpm=float(row.requests),
        tpm=float(row.total_tokens) if row.usage_samples else None,
        native_usd_per_minute=row.native_spend_usd if row.spend_samples else None,
    )


def _safe_error_type(value: str) -> str:
    return value if value in _SAFE_ERROR_TYPES else "UnknownError"


def _safe_status(value: str | None) -> int | None:
    return int(value) if value is not None and re.fullmatch(r"[1-5][0-9]{2}", value) else None


def safe_error(row: _ErrorRow) -> SafeMetricsError:
    error_type: Final = _safe_error_type(row.error_type)
    status: Final = _safe_status(row.status_code)
    label: Final = "Request failed" if row.source == "request" else "Upstream error event"
    suffix: Final = f", HTTP {status}" if status is not None else ""
    if row.time is None:
        raise ValueError("Recent error requires a timestamp")
    return SafeMetricsError(
        time=row.time.replace(tzinfo=timezone.utc) if row.time.tzinfo is None else row.time,
        source=row.source,
        request_id=row.request_id,
        model=row.model,
        error_type=error_type,
        status_code=status,
        message=f"{label} ({error_type}{suffix})",
    )


def _safe_groups(
    rows: Sequence[_ErrorRow], source: Literal["request", "upstream_event"]
) -> tuple[MetricsErrorGroup, ...]:
    matching: Final = tuple(item for item in rows if item.source == source)
    keys: Final = frozenset((_safe_error_type(item.error_type), _safe_status(item.status_code)) for item in matching)
    grouped: Final = (
        MetricsErrorGroup(
            error_type=kind,
            status_code=status,
            count=sum(
                item.count
                for item in matching
                if (_safe_error_type(item.error_type), _safe_status(item.status_code)) == (kind, status)
            ),
        )
        for kind, status in keys
    )
    return tuple(sorted(grouped, key=lambda item: (-item.count, item.error_type, item.status_code or 0)))


def _integer_json(path: str) -> str:
    return f"CASE WHEN ({path}) ~ '^[0-9]{{1,18}}$' THEN ({path})::bigint END"


def _usage_count(paths: tuple[str, ...]) -> str:
    counts: Final = (_integer_json(f"s.metadata #>> '{{additional_usage_values,{path}}}'") for path in paths)
    return "GREATEST(" + ",".join(counts) + ")"


_SAFE_ERROR_TYPES: Final = frozenset(
    {
        "APIError",
        "APIConnectionError",
        "APITimeoutError",
        "AuthenticationError",
        "BadRequestError",
        "BudgetExceededError",
        "ContentPolicyViolationError",
        "ContextWindowExceededError",
        "InternalServerError",
        "NotFoundError",
        "PermissionDeniedError",
        "RateLimitError",
        "ServiceUnavailableError",
        "Timeout",
        "TimeoutError",
        "UnprocessableEntityError",
        "UnknownError",
        "UnsupportedParamsError",
        "ValueError",
        "ValidationError",
        "HTTPException",
        "ProxyException",
    }
)
_SAFE_ERROR_SQL: Final = ",".join("'" + name + "'" for name in sorted(_SAFE_ERROR_TYPES))


def _error_type_sql(path: str) -> str:
    return f"CASE WHEN ({path}) IN ({_SAFE_ERROR_SQL}) THEN ({path}) ELSE 'UnknownError' END"


def _status_sql(path: str) -> str:
    return f"CASE WHEN ({path}) ~ '^[1-5][0-9]{{2}}$' THEN ({path}) END"


_REQUEST_ERROR_TYPE: Final = _error_type_sql("s.metadata #>> '{error_information,error_class}'")
_REQUEST_STATUS: Final = _status_sql("s.metadata #>> '{error_information,error_code}'")
_UPSTREAM_ERROR_TYPE: Final = _error_type_sql("e.exception_type")
_UPSTREAM_STATUS: Final = _status_sql("e.status_code")
_HELPER: Final = """COALESCE(s.request_id ~ '-attempt-[0-9]+$' AND
    substring(s.request_id from '-attempt-[0-9]+$') =
    substring(COALESCE(s.litellm_call_id,'') from '-attempt-[0-9]+$'),false)"""
_READ_ALIASES: Final = _usage_count(tuple(
    f"{prefix},{field}" if prefix else field
    for prefix in ("", *CACHE_DETAIL_FIELDS) for field in CACHE_READ_FIELDS
))
_READ_CANONICAL: Final = _usage_count((
    "cache_read_input_tokens", *(f"{prefix},cached_tokens" for prefix in CACHE_DETAIL_FIELDS),
))
_READ: Final = f"COALESCE(NULLIF({_READ_CANONICAL},0),{_READ_ALIASES})"
_WRITE: Final = _usage_count(tuple(
    f"{prefix},{field}" if prefix else field
    for prefix in ("", *CACHE_DETAIL_FIELDS) for field in CACHE_WRITE_FIELDS
))
_REASONING: Final = _integer_json(
    "s.metadata #>> '{additional_usage_values,completion_tokens_details,reasoning_tokens}'"
)
_BASE: Final = f"""WITH observed AS (
 SELECT m.channel_id, COALESCE(NULLIF(s.model_group,''),NULLIF(s.model,''),'Unknown') AS model,
 s.\"startTime\" AS started, s.\"endTime\" AS ended, s.request_id,
 lower(s.status) AS status, {_HELPER} AS helper,
 CASE WHEN s.prompt_tokens>=0 THEN s.prompt_tokens END AS input_tokens,
 CASE WHEN s.completion_tokens>=0 THEN s.completion_tokens END AS output_tokens,
 CASE WHEN s.total_tokens>=0 THEN s.total_tokens END AS total_tokens,
 COALESCE(co.cache_read_tokens,{_READ}) AS cache_read_tokens,
 COALESCE(co.cache_write_tokens,legacy_cache.cache_write_tokens) AS cache_write_tokens,
 COALESCE(co.cache_write_ttl_tokens,legacy_cache.cache_write_ttl_tokens) AS cache_write_ttl_tokens,
 COALESCE(co.cache_write_unclassified_tokens,legacy_cache.cache_write_unclassified_tokens)
      AS cache_write_unclassified_tokens,
 {_REASONING} AS reasoning_tokens,
 CASE WHEN lower(s.cache_hit) IN ('true','false') THEN lower(s.cache_hit)='true' END AS cache_hit,
 CASE WHEN jsonb_typeof(s.metadata->'observed_stream')='boolean'
      THEN (s.metadata->>'observed_stream')::boolean END AS stream,
 CASE WHEN s.spend::text NOT IN ('NaN','Infinity','-Infinity') THEN s.spend::numeric END AS spend,
 CASE WHEN s.request_duration_ms>=0 THEN s.request_duration_ms::double precision
      WHEN s.\"endTime\">=s.\"startTime\" THEN extract(epoch FROM (s.\"endTime\"-s.\"startTime\"))*1000 END AS latency,
 CASE WHEN s.\"completionStartTime\">=s.\"startTime\" AND s.\"completionStartTime\"<=s.\"endTime\"
      THEN extract(epoch FROM (s.\"completionStartTime\"-s.\"startTime\"))*1000 END AS ttft,
 {_REQUEST_ERROR_TYPE} AS error_type, {_REQUEST_STATUS} AS status_code
 FROM \"LiteLLM_SpendLogs\" s
 JOIN retail_channel_deployments m ON m.deployment_id=s.model_id
 LEFT JOIN retail_cache_observations co ON co.litellm_call_id=s.litellm_call_id
      AND co.user_id=COALESCE(s.\"user\",'') AND co.started_at=date_trunc('milliseconds',s.\"startTime\")
 {legacy_cache_sql(_WRITE)}
 WHERE s.\"startTime\">=%s AND s.\"startTime\"<%s
 AND (%s::text IS NULL OR m.channel_id=%s)
), samples AS (
 SELECT *, to_timestamp(floor(extract(epoch FROM started)/(%s*60))*(%s*60)) AS bucket,
 CASE WHEN NOT helper THEN latency END AS request_latency,
 CASE WHEN NOT helper AND stream IS TRUE AND status='success' AND cache_hit IS NOT TRUE
      THEN ttft END AS stream_ttft FROM observed
)"""

_FINAL_TTL_AGGREGATE: Final = TTL_AGGREGATE.replace(
    "array_agg(cache_write_ttl_tokens)", "array_agg(cache_write_ttl_tokens) FILTER(WHERE NOT helper)"
)
_AGGREGATES: Final = f"""
 count(*) FILTER(WHERE NOT helper) AS requests,
 count(*) FILTER(WHERE NOT helper AND status='success') AS successful_requests,
 count(*) FILTER(WHERE NOT helper AND status='failure') AS failed_requests,
 count(*) FILTER(WHERE NOT helper AND (status IS NULL OR status NOT IN ('success','failure'))) AS unknown_status_requests,
 sum(input_tokens)::bigint AS input_tokens, sum(output_tokens)::bigint AS output_tokens,
 sum(total_tokens)::bigint AS total_tokens,
 sum(cache_read_tokens) FILTER(WHERE NOT helper)::bigint AS cache_read_tokens,
 sum(cache_write_tokens) FILTER(WHERE NOT helper)::bigint AS cache_write_tokens,
 sum(reasoning_tokens)::bigint AS reasoning_tokens,
 sum(cache_write_unclassified_tokens) FILTER(WHERE NOT helper)::bigint AS cache_write_unclassified_tokens,
 {_FINAL_TTL_AGGREGATE},
 count(*) FILTER(WHERE NOT helper AND cache_hit IS TRUE) AS cache_hit_requests,
 count(cache_hit) FILTER(WHERE NOT helper) AS cache_status_samples,
 count(*) FILTER(WHERE NOT helper AND stream IS TRUE) AS streaming_requests,
 count(*) FILTER(WHERE helper) AS billed_attempts,
 count(total_tokens) AS usage_samples, count(spend) AS spend_samples,
 sum(spend)::text AS native_spend_usd,
 count(request_latency) AS latency_samples, count(stream_ttft) AS ttft_samples,
 avg(request_latency)::double precision AS latency_avg_ms,
 percentile_cont(0.5) WITHIN GROUP(ORDER BY request_latency)::double precision AS latency_p50_ms,
 percentile_cont(0.95) WITHIN GROUP(ORDER BY request_latency)::double precision AS latency_p95_ms,
 avg(stream_ttft)::double precision AS ttft_avg_ms,
 percentile_cont(0.5) WITHIN GROUP(ORDER BY stream_ttft)::double precision AS ttft_p50_ms,
 percentile_cont(0.95) WITHIN GROUP(ORDER BY stream_ttft)::double precision AS ttft_p95_ms
"""
_CHANNELS: Final = """SELECT c.id,c.name,c.pool_id,p.name AS pool_name,c.status,
 (SELECT count(*) FROM retail_channel_deployments m WHERE m.channel_id=c.id) AS deployment_count
 FROM retail_channels c JOIN retail_resource_pools p ON p.id=c.pool_id
 WHERE (%s::text IS NULL OR c.id=%s) ORDER BY c.name,c.id"""
_COVERAGE: Final = f"""SELECT count(*) AS window_log_rows,
 count(*) FILTER(WHERE NOT ({_HELPER})) AS final_requests,
 count(*) FILTER(WHERE NOT ({_HELPER}) AND m.channel_id IS NOT NULL) AS attributed_requests,
 count(*) FILTER(WHERE NULLIF(s.model_id,'') IS NOT NULL) AS identified_rows
 FROM \"LiteLLM_SpendLogs\" s LEFT JOIN retail_channel_deployments m ON m.deployment_id=s.model_id
 WHERE s.\"startTime\">=%s AND s.\"startTime\"<%s"""
_ERRORS: Final = f"""WITH errors AS (
 SELECT m.channel_id,'upstream_event'::text AS source,{_UPSTREAM_ERROR_TYPE} AS error_type,
 {_UPSTREAM_STATUS} AS status_code,e.\"startTime\" AS time,e.request_id,
 COALESCE(NULLIF(e.model_group,''),'Unknown') AS model
 FROM \"LiteLLM_ErrorLogs\" e JOIN retail_channel_deployments m ON m.deployment_id=e.model_id
 WHERE e.\"startTime\">=%s AND e.\"startTime\"<%s AND (%s::text IS NULL OR m.channel_id=%s)
) """


def _aggregate_sql(detail: bool) -> str:
    dimensions: Final = (
        "CASE WHEN grouping(model)=0 THEN 'model' WHEN grouping(bucket)=0 THEN 'trend' ELSE 'channel' END AS kind,"
        "model,bucket"
        if detail
        else "'channel'::text AS kind,NULL::text AS model,NULL::timestamptz AS bucket"
    )
    grouping: Final = "GROUPING SETS ((channel_id),(channel_id,model),(channel_id,bucket))" if detail else "channel_id"
    return _BASE + f" SELECT channel_id,{dimensions},{_AGGREGATES} FROM samples GROUP BY {grouping}"


async def _read_aggregates(
    connection: psycopg.AsyncConnection,
    start: datetime,
    end: datetime,
    channel_id: str | None,
    bucket_minutes: int,
    detail: bool,
) -> tuple[_Aggregate, ...]:
    return await _many(
        connection,
        _aggregate_sql(detail),
        (start, end, channel_id, channel_id, bucket_minutes, bucket_minutes),
        _Aggregate,
    )


def _find(rows: Sequence[_Aggregate], channel_id: str, model: str | None = None) -> _Aggregate | None:
    kind: Final = "channel" if model is None else "model"
    return next(
        (item for item in rows if item.channel_id == channel_id and item.kind == kind and item.model == model), None
    )


def _coverage(row: _CoverageRow) -> MetricsCoverage:
    return MetricsCoverage(
        window_log_rows=row.window_log_rows,
        attributed_requests=row.attributed_requests,
        unattributed_requests=row.final_requests - row.attributed_requests,
        model_id_coverage=row.identified_rows / row.window_log_rows if row.window_log_rows else None,
    )


def _identity_activity(
    channel: ChannelIdentity, period: Sequence[_Aggregate], rolling: Sequence[_Aggregate], errors: Sequence[_ErrorRow]
) -> ChannelActivity:
    return ChannelActivity(
        **channel.model_dump(),
        metrics=period_metrics(_find(period, channel.id)),
        rolling=rolling_metrics(_find(rolling, channel.id)),
        upstream_error_events=sum(
            item.count for item in errors if item.channel_id == channel.id and item.source == "upstream_event"
        ),
    )


async def load_channel_metrics(
    store: WalletStore,
    hours: Hours,
    channel_id: str | None,
    now: datetime,
) -> ChannelMetricsPage | ChannelMetricsDetail:
    window: Final = metrics_window(hours, now)
    start: Final = window.start.astimezone(timezone.utc).replace(tzinfo=None)
    end: Final = window.end.astimezone(timezone.utc).replace(tzinfo=None)
    rolling_start: Final = end - timedelta(seconds=60)
    async with store.transaction() as connection:
        await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        await connection.execute("SET LOCAL statement_timeout = '15000ms'")
        channels: Final = await _many(connection, _CHANNELS, (channel_id, channel_id), ChannelIdentity)
        if channel_id is not None and not channels:
            raise HTTPException(404, "Channel does not exist")
        period: Final = await _read_aggregates(
            connection, start, end, channel_id, window.bucket_minutes, channel_id is not None
        )
        rolling: Final = await _read_aggregates(connection, rolling_start, end, channel_id, 1, channel_id is not None)
        coverage_rows: Final = await _many(connection, _COVERAGE, (start, end), _CoverageRow)
        upstream: Final = await _many(
            connection,
            _ERRORS + "SELECT channel_id,source,error_type,status_code,count(*) AS count FROM errors "
            "GROUP BY channel_id,source,error_type,status_code",
            (start, end, channel_id, channel_id),
            _ErrorRow,
        )
        coverage: Final = _coverage(coverage_rows[0])
        if channel_id is None:
            return ChannelMetricsPage(
                window=window,
                channels=tuple(_identity_activity(item, period, rolling, upstream) for item in channels),
                coverage=coverage,
                notes=_NOTES,
            )
        failed_rows: Final = await _many(
            connection,
            _BASE + " SELECT channel_id,'request'::text AS source,COALESCE(error_type,'UnknownError') AS "
            "error_type,status_code,count(*) AS count FROM samples WHERE NOT helper AND status='failure' "
            "GROUP BY channel_id,error_type,status_code",
            (start, end, channel_id, channel_id, window.bucket_minutes, window.bucket_minutes),
            _ErrorRow,
        )
        recent_requests: Final = await _many(
            connection,
            _BASE + " SELECT channel_id,'request'::text AS source,COALESCE(error_type,'UnknownError') AS "
            "error_type,status_code,started AS time,request_id,model FROM samples "
            "WHERE NOT helper AND status='failure' ORDER BY started DESC,request_id DESC LIMIT 20",
            (start, end, channel_id, channel_id, window.bucket_minutes, window.bucket_minutes),
            _ErrorRow,
        )
        recent_upstream: Final = await _many(
            connection,
            _ERRORS + "SELECT * FROM errors ORDER BY time DESC,request_id DESC LIMIT 20",
            (start, end, channel_id, channel_id),
            _ErrorRow,
        )
        activity: Final = _identity_activity(channels[0], period, rolling, upstream)
        recent: Final = tuple(safe_error(item) for item in (*recent_requests, *recent_upstream))
        return ChannelMetricsDetail(
            window=window,
            channel=channels[0],
            metrics=activity.metrics,
            rolling=activity.rolling,
            upstream_error_events=activity.upstream_error_events,
            models=tuple(
                ModelActivity(
                    model=item.model or "Unknown",
                    metrics=period_metrics(item),
                    rolling=rolling_metrics(_find(rolling, channel_id, item.model)),
                )
                for item in period
                if item.kind == "model"
            ),
            trend=tuple(
                MetricsTrend(time=item.bucket, metrics=period_metrics(item))
                for item in sorted(period, key=lambda entry: entry.bucket or window.start)
                if item.kind == "trend" and item.bucket is not None
            ),
            request_error_groups=_safe_groups(failed_rows, "request"),
            upstream_error_groups=_safe_groups(upstream, "upstream_event"),
            recent_errors=tuple(sorted(recent, key=lambda item: item.time, reverse=True)[:20]),
            coverage=coverage,
            notes=_NOTES,
        )


@dataclass(frozen=True, slots=True)
class _CachedMetrics:
    expires: float
    page: ChannelMetricsPage | ChannelMetricsDetail


class _MetricsCache:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: dict[tuple[str, Hours, str | None], _CachedMetrics] = {}

    async def get(
        self,
        key: tuple[str, Hours, str | None],
        loader: Callable[[], Awaitable[ChannelMetricsPage | ChannelMetricsDetail]],
    ) -> ChannelMetricsPage | ChannelMetricsDetail:
        async with self._lock:
            now: Final = monotonic()
            cached: Final = self._entries.get(key)
            if cached is not None and cached.expires > now:
                return cached.page
            page: Final = await loader()
            alive: Final = {item: value for item, value in self._entries.items() if value.expires > now}
            self._entries = {**dict(tuple(alive.items())[-63:]), key: _CachedMetrics(monotonic() + 8, page)}
            return page


_CACHE: Final = _MetricsCache()


async def _page(store: WalletStore, hours: Hours, channel_id: str | None) -> ChannelMetricsPage | ChannelMetricsDetail:
    key: Final = (sha256(store.database_url.encode()).hexdigest(), hours, channel_id)
    try:
        return await asyncio.wait_for(
            _CACHE.get(key, lambda: load_channel_metrics(store, hours, channel_id, datetime.now(timezone.utc))),
            timeout=25,
        )
    except (psycopg.Error, asyncio.TimeoutError) as exc:
        raise HTTPException(503, "Channel activity storage is temporarily unavailable") from exc


@router.get("/metrics", response_model=ChannelMetricsPage)
async def admin_channel_metrics(admin: Admin, store: Store, hours: Hours = Hours.DAY) -> ChannelMetricsPage:
    page: Final = await _page(store, hours, None)
    if isinstance(page, ChannelMetricsPage):
        return page
    raise HTTPException(503, "Channel activity is temporarily unavailable")


@router.get("/{channel_id}/metrics", response_model=ChannelMetricsDetail)
async def admin_channel_metric_detail(
    channel_id: str, admin: Admin, store: Store, hours: Hours = Hours.DAY
) -> ChannelMetricsDetail:
    page: Final = await _page(store, hours, channel_id)
    if isinstance(page, ChannelMetricsDetail):
        return page
    raise HTTPException(503, "Channel activity is temporarily unavailable")
