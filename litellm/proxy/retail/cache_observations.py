import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Final

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import get_litellm_metadata_from_kwargs
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

from .cache_usage import CACHE_DETAIL_FIELDS, CacheUsage, cache_usage, has_cache_fields
from .pricing import as_mapping, response_usage, usage_facts
from .store import WalletStore, _many, get_store

_MAX_PENDING: Final = 64
TTL_AGGREGATE: Final = """(SELECT COALESCE(jsonb_object_agg(ttl,tokens),'{}'::jsonb) FROM (
 SELECT e.key AS ttl,sum(e.value::bigint)::bigint AS tokens
 FROM unnest(array_agg(cache_write_ttl_tokens)) d(payload)
 CROSS JOIN LATERAL jsonb_each_text(d.payload) e GROUP BY e.key
 ) ttl_totals) AS cache_write_ttl_tokens"""


def legacy_cache_sql(write_total: str) -> str:
    """Read only explicit native duration fields; never infer duration from model/provider."""
    sources: Final = tuple(
        (ttl, f"{prefix},{container}" if prefix else container, field)
        for prefix in ("", *CACHE_DETAIL_FIELDS)
        for container in ("cache_creation", "cache_creation_token_details", "cache_write_tokens_details")
        for ttl, field in (
            ("300", "ephemeral_5m_input_tokens"), ("3600", "ephemeral_1h_input_tokens"),
            ("1800", "ttl_30m_tokens"), ("43200", "ttl_12h_tokens"),
        )
    )
    values: Final = ",".join(
        f"('{ttl}',s.metadata #> '{{additional_usage_values,{container},{field}}}')"
        for ttl, container, field in sources
    )
    return f"""LEFT JOIN LATERAL (
 SELECT total::bigint AS cache_write_tokens,
 CASE WHEN valid AND reported_total<=total
      THEN tiers ELSE '{{}}'::jsonb END AS cache_write_ttl_tokens,
 CASE WHEN valid AND reported_total<=total
      THEN total-reported_total ELSE total END::bigint
      AS cache_write_unclassified_tokens
 FROM (SELECT *,COALESCE(NULLIF({write_total},0),CASE WHEN valid THEN reported_total END,{write_total}) AS total
       FROM (SELECT sum(tokens)::bigint AS reported_total,
       COALESCE(bool_and(valid),true) AS valid,
       COALESCE(jsonb_object_agg(ttl,tokens) FILTER(WHERE tokens>0),'{{}}'::jsonb) AS tiers
       FROM (SELECT ttl,max(tokens)::bigint AS tokens,bool_and(valid) AS valid
             FROM (SELECT ttl,
                   CASE WHEN jsonb_typeof(amount)='number' AND amount::text ~ '^[0-9]{{1,18}}$'
                        THEN amount::text::bigint END AS tokens,
                   amount IS NULL OR amount='null'::jsonb OR
                   (jsonb_typeof(amount)='number' AND amount::text ~ '^[0-9]{{1,18}}$') AS valid
                   FROM (VALUES {values}) native(ttl,amount)) parsed GROUP BY ttl) durations) aggregate) normalized
 ) legacy_cache ON true"""


class CacheObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    litellm_call_id: str = Field(min_length=1, max_length=191)
    user_id: str = Field(min_length=1, max_length=191)
    started_at: datetime
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    cache_write_ttl_tokens: dict[str, int]
    cache_write_unclassified_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_cache_categories(self) -> "CacheObservation":
        usage: Final = CacheUsage.model_validate(self.model_dump())
        if usage.cache_write_unclassified_tokens != self.cache_write_unclassified_tokens:
            raise ValueError("Cache duration subsets and unspecified writes must equal the write total")
        return self


class _CacheLogRow(CacheObservation):
    request_id: str


def _utc_naive(value: datetime) -> datetime:
    result: Final = value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)
    return result.replace(microsecond=result.microsecond // 1000 * 1000)


def _has_cache_fields(usage: Mapping[str, object]) -> bool:
    return has_cache_fields(usage)


def cache_observation(kwargs: Mapping[str, object], response: object, start_time: datetime) -> CacheObservation | None:
    call_id: Final = kwargs.get("litellm_call_id")
    metadata: Final = as_mapping(get_litellm_metadata_from_kwargs(dict(kwargs)))
    user_id: Final = metadata.get("user_api_key_user_id")
    if not isinstance(call_id, str) or not 0 < len(call_id) <= 191:
        return None
    if not isinstance(user_id, str) or not 0 < len(user_id) <= 191 or not isinstance(start_time, datetime):
        return None
    usage: Final = response_usage(response)
    if not _has_cache_fields(usage):
        return None
    facts: Final = usage_facts(response, allow_input_only=True)
    if facts is None:
        return None
    return CacheObservation(
        litellm_call_id=call_id, user_id=user_id, started_at=_utc_naive(start_time),
        cache_read_tokens=facts.cache_read_tokens, cache_write_tokens=facts.cache_write_tokens,
        cache_write_ttl_tokens=facts.cache_write_ttl_tokens,
        cache_write_unclassified_tokens=facts.cache_write_unclassified_tokens,
    )


async def persist_cache_observation(observation: CacheObservation, store: WalletStore | None = None) -> None:
    target: Final = store if store is not None else get_store()
    await target.initialize()
    async with target.transaction() as connection:
        await connection.execute(
            """INSERT INTO retail_cache_observations
            (litellm_call_id,user_id,started_at,cache_read_tokens,cache_write_tokens,
             cache_write_ttl_tokens,cache_write_unclassified_tokens)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (litellm_call_id,user_id,started_at) DO NOTHING""",
            (
                observation.litellm_call_id, observation.user_id, observation.started_at,
                observation.cache_read_tokens, observation.cache_write_tokens,
                Jsonb(observation.cache_write_ttl_tokens), observation.cache_write_unclassified_tokens,
            ),
        )


class CacheObservationCollector(CustomLogger):
    def __init__(self, writer: Callable[[CacheObservation], Awaitable[None]] = persist_cache_observation) -> None:
        super().__init__()
        self._writer = writer
        self._pending: set[asyncio.Task[None]] = set()
        self._slots = asyncio.Semaphore(2)

    async def async_log_success_event(
        self, kwargs: Mapping[str, object], response_obj: object, start_time: datetime, end_time: datetime,
    ) -> None:
        if len(self._pending) >= _MAX_PENDING:
            return
        response: Final = kwargs.get("complete_streaming_response") or response_obj
        try:
            observation: Final = cache_observation(kwargs, response, start_time)
        except (ValueError, TypeError):
            return
        if observation is not None:
            task: Final = asyncio.create_task(self._write_safely(observation))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _write_safely(self, observation: CacheObservation) -> None:
        try:
            await asyncio.wait_for(self._bounded_write(observation), timeout=2)
        except Exception as exc:
            verbose_proxy_logger.warning("Cache observation could not be saved (%s)", type(exc).__name__)

    async def _bounded_write(self, observation: CacheObservation) -> None:
        async with self._slots:
            await self._writer(observation)

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)


def cache_usage_view(value: object) -> dict[str, JsonValue] | None:
    row: Final = as_mapping(value)
    if any(key not in row for key in ("cache_read_tokens", "cache_write_tokens", "cache_write_unclassified_tokens")):
        return None
    unknown: Final = row["cache_write_unclassified_tokens"]
    if not isinstance(unknown, int) or isinstance(unknown, bool) or unknown < 0:
        return None
    try:
        usage: Final = CacheUsage.model_validate(row)
    except ValidationError:
        return None
    if row["cache_write_unclassified_tokens"] != usage.cache_write_unclassified_tokens:
        return None
    return usage.model_dump()


def _log_rows(payload: object) -> tuple[Mapping[str, object], ...]:
    rows: Final = payload if isinstance(payload, (tuple, list)) else as_mapping(payload).get("data")
    if isinstance(rows, (tuple, list)):
        return tuple(as_mapping(row) for row in rows)
    row: Final = as_mapping(payload)
    return (row,) if row.get("request_id") else ()


def _row_start(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _utc_naive(value)
    if isinstance(value, str):
        try:
            return _utc_naive(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def legacy_cache_usage(row: Mapping[str, object]) -> dict[str, JsonValue] | None:
    raw: Final = row.get("metadata")
    try:
        metadata: Final = as_mapping(json.loads(raw)) if isinstance(raw, str) else as_mapping(raw)
    except (ValueError, TypeError):
        return None
    additional: Final = as_mapping(metadata.get("additional_usage_values"))
    if not _has_cache_fields(additional):
        return None
    try:
        return cache_usage(additional).model_dump()
    except ValueError:
        return None


def _enriched_row(row: Mapping[str, object], observations: Sequence[_CacheLogRow]) -> dict[str, object]:
    started: Final = _row_start(row.get("startTime"))
    matched: Final = tuple(
        item for item in observations
        if item.request_id == row.get("request_id") and (started is None or item.started_at == started)
    )
    view: Final = cache_usage_view(matched[0]) if len(matched) == 1 else legacy_cache_usage(row)
    return {**row, **({"cache_usage": view} if view is not None else {})}


def enrich_log_payload(payload: object, observations: Sequence[_CacheLogRow]) -> object:
    if isinstance(payload, (tuple, list)):
        return [_enriched_row(as_mapping(row), observations) for row in payload]
    page: Final = as_mapping(payload)
    rows: Final = page.get("data")
    if isinstance(rows, (tuple, list)):
        return {**page, "data": [_enriched_row(as_mapping(row), observations) for row in rows]}
    return _enriched_row(page, observations) if page.get("request_id") else payload


async def enrich_cache_logs(payload: object, auth: UserAPIKeyAuth, store: WalletStore | None = None) -> object:
    if os.getenv("RETAIL_PORTAL_ENABLED", "false").lower() != "true":
        return payload
    rows: Final = _log_rows(payload)
    request_ids: Final = tuple(
        dict.fromkeys(
            row["request_id"] for row in rows
            if isinstance(row.get("request_id"), str) and 0 < len(row["request_id"]) <= 191
        )
    )
    if not request_ids or len(request_ids) > 500:
        return payload
    admin: Final = auth.user_role in {LitellmUserRoles.PROXY_ADMIN, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY}
    owner: Final = None if admin else auth.user_id
    if not admin and not owner:
        return payload
    try:
        target: Final = store if store is not None else get_store()
        async with target.transaction() as connection:
            await connection.execute("SET TRANSACTION READ ONLY")
            await connection.execute("SET LOCAL statement_timeout = '3000ms'")
            observations: Final = await _many(
                connection,
                """SELECT s.request_id,o.* FROM \"LiteLLM_SpendLogs\" s
                JOIN retail_cache_observations o ON o.litellm_call_id=s.litellm_call_id
                AND o.user_id=COALESCE(s.\"user\",'') AND o.started_at=date_trunc('milliseconds',s.\"startTime\")
                WHERE s.request_id=ANY(%s::text[]) AND (%s::text IS NULL OR s.\"user\"=%s)""",
                (list(request_ids), owner, owner), _CacheLogRow,
            )
        return enrich_log_payload(payload, observations)
    except Exception as exc:
        verbose_proxy_logger.warning("Cache log observations unavailable (%s)", type(exc).__name__)
        return payload


cache_observation_callback: Final = CacheObservationCollector()
