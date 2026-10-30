from datetime import datetime, timedelta, timezone
from typing import Final

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail.api import retail_admin
from litellm.proxy.retail.channel_metrics import (
    ChannelIdentity,
    Hours,
    _Aggregate,
    _coverage,
    _CoverageRow,
    _ErrorRow,
    _identity_activity,
    _safe_groups,
    metrics_window,
    period_metrics,
    rolling_metrics,
    safe_error,
)

_NOW: Final = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("hours", tuple(Hours))
def test_time_window_has_exact_duration_and_bounded_trend_buckets(hours: Hours) -> None:
    window: Final = metrics_window(hours, _NOW)
    assert window.end == _NOW
    assert window.end - window.start == timedelta(hours=hours)
    assert 1 <= window.bucket_minutes <= 60
    assert hours * 60 / window.bucket_minutes <= 720
    assert window.rolling_seconds == 60


@pytest.mark.parametrize("hours", [0, 2, 48, 721, -1, "-1; DROP TABLE logs"])
def test_only_explicitly_supported_windows_are_accepted(hours: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Hours).validate_python(hours)


def test_error_rate_uses_only_final_request_denominator_and_never_retry_event_counts() -> None:
    row: Final = _Aggregate(
        channel_id="one",
        requests=4,
        successful_requests=3,
        failed_requests=1,
        billed_attempts=7,
        native_spend_usd="0.000120000000000000001",
        spend_samples=11,
    )
    metrics: Final = period_metrics(row)
    assert metrics.error_rate_pct == 25
    assert metrics.requests == 4
    assert metrics.billed_attempts == 7
    assert metrics.native_spend_usd == row.native_spend_usd
    assert metrics.spend_samples == 11


def test_unknown_status_and_cache_state_are_not_reported_as_success_or_miss() -> None:
    row: Final = _Aggregate(
        channel_id="one",
        requests=4,
        failed_requests=1,
        unknown_status_requests=1,
        cache_hit_requests=1,
        cache_status_samples=3,
    )
    metrics: Final = period_metrics(row)
    assert metrics.error_rate_pct is None
    assert metrics.cache_hit_rate_pct is None
    complete: Final = period_metrics(row.model_copy(update={"unknown_status_requests": 0, "cache_status_samples": 4}))
    assert complete.error_rate_pct == 25
    assert complete.cache_hit_rate_pct == 25


def test_missing_samples_remain_unknown_but_recorded_zero_is_preserved() -> None:
    empty: Final = period_metrics(None)
    assert empty.requests == 0
    assert empty.input_tokens is None
    assert empty.native_spend_usd is None
    assert empty.ttft_avg_ms is None
    assert empty.cache_read_tokens is None
    recorded: Final = period_metrics(
        _Aggregate(
            channel_id="one",
            input_tokens=0,
            total_tokens=0,
            usage_samples=1,
            cache_read_tokens=0,
            reasoning_tokens=0,
            native_spend_usd="0",
            spend_samples=1,
        )
    )
    assert recorded.input_tokens == 0
    assert recorded.cache_read_tokens == 0
    assert recorded.reasoning_tokens == 0
    assert recorded.native_spend_usd == "0"


def test_rolling_rates_use_only_the_independent_60_second_aggregate() -> None:
    row: Final = _Aggregate(
        channel_id="one", requests=3, total_tokens=60, usage_samples=3, native_spend_usd="0.00003", spend_samples=3
    )
    rolling: Final = rolling_metrics(row)
    assert rolling.window_seconds == 60
    assert rolling.rpm == 3
    assert rolling.tpm == 60
    assert rolling.native_usd_per_minute == "0.00003"
    assert rolling_metrics(None).rpm == 0
    assert rolling_metrics(_Aggregate(channel_id="one", billed_attempts=1)).rpm == 0
    assert rolling_metrics(_Aggregate(channel_id="one", requests=3)).tpm is None


def test_same_public_model_does_not_merge_distinct_channels_or_error_events() -> None:
    channel: Final = ChannelIdentity(
        id="first", name="First", pool_id="pool", pool_name="Pool", status="active", deployment_count=1
    )
    period: Final = (
        _Aggregate(channel_id="first", requests=3, successful_requests=3),
        _Aggregate(channel_id="second", requests=7, failed_requests=7),
        _Aggregate(channel_id="first", kind="model", model="shared", requests=3),
        _Aggregate(channel_id="second", kind="model", model="shared", requests=7),
    )
    events: Final = (
        _ErrorRow(channel_id="first", source="upstream_event", error_type="TimeoutError", status_code="504", count=9),
        _ErrorRow(channel_id="second", source="upstream_event", error_type="TimeoutError", status_code="504", count=50),
    )
    view: Final = _identity_activity(channel, period, (), events)
    assert view.metrics.requests == 3
    assert view.metrics.error_rate_pct == 0
    assert view.upstream_error_events == 9
    assert view.rolling.rpm == 0


@pytest.mark.parametrize(
    "unsafe",
    [
        "sk-private-token",
        "https://secret-upstream.invalid",
        "<html>secret</html>",
        "eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "A" * 99,
        "SecretCredentialError",
    ],
)
def test_recent_error_never_echoes_arbitrary_exception_labels(unsafe: str) -> None:
    error: Final = safe_error(
        _ErrorRow(
            channel_id="first",
            source="request",
            error_type=unsafe,
            status_code="401 secret",
            time=_NOW,
            request_id="request",
            model="shared",
        )
    )
    assert error.error_type == "UnknownError"
    assert error.status_code is None
    assert unsafe not in error.model_dump_json()
    assert error.message == "Request failed (UnknownError)"


def test_recent_error_preserves_safe_class_http_code_and_utc_time() -> None:
    error: Final = safe_error(
        _ErrorRow(
            channel_id="first",
            source="upstream_event",
            error_type="RateLimitError",
            status_code="429",
            time=_NOW.replace(tzinfo=None),
            request_id="request",
            model="shared",
        )
    )
    assert error.error_type == "RateLimitError"
    assert error.status_code == 429
    assert error.time == _NOW
    assert error.message == "Upstream error event (RateLimitError, HTTP 429)"


def test_grouping_redacts_unknown_types_and_keeps_request_failures_separate_from_attempt_events() -> None:
    rows: Final = (
        _ErrorRow(channel_id="first", source="request", error_type="private-one", status_code="500", count=2),
        _ErrorRow(channel_id="first", source="request", error_type="private-two", status_code="500", count=3),
        _ErrorRow(channel_id="first", source="upstream_event", error_type="TimeoutError", status_code="504", count=7),
    )
    request: Final = _safe_groups(rows, "request")
    upstream: Final = _safe_groups(rows, "upstream_event")
    assert len(request) == 1
    assert request[0].count == 5
    assert request[0].error_type == "UnknownError"
    assert upstream[0].count == 7
    assert upstream[0].error_type == "TimeoutError"


def test_gateway_coverage_keeps_missing_deployment_ids_unattributed() -> None:
    view: Final = _coverage(
        _CoverageRow(window_log_rows=12, final_requests=10, attributed_requests=4, identified_rows=6)
    )
    assert view.scope == "gateway"
    assert view.model_id_coverage == 0.5
    assert view.attributed_requests == 4
    assert view.unattributed_requests == 6
    empty: Final = _coverage(
        _CoverageRow(window_log_rows=0, final_requests=0, attributed_requests=0, identified_rows=0)
    )
    assert empty.model_id_coverage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY])
async def test_native_user_and_read_only_admin_cannot_access_channel_metrics(role: LitellmUserRoles) -> None:
    auth: Final = UserAPIKeyAuth(user_id="account", user_role=role)
    with pytest.raises(HTTPException) as error:
        await retail_admin(auth)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_proxy_admin_passes_existing_server_permission_guard() -> None:
    auth: Final = UserAPIKeyAuth(user_id="admin", user_role=LitellmUserRoles.PROXY_ADMIN)
    assert await retail_admin(auth) is auth
