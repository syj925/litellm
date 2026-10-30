from datetime import datetime, timedelta, timezone
from typing import Final

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from litellm.proxy.retail.model_metrics import (
    ModelMetricHours,
    ModelMetricsPage,
    _Aggregate,
    _MetricsCache,
    metrics_page,
    model_metrics_window,
    selected_deployment_ids,
    summarize,
)
from litellm.proxy.retail.pool_store import PoolAccess, PoolGrant, _PoolRow, _StoredBinding

_NOW: Final = datetime(2026, 1, 2, 12, 7, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _pool(identifier: str, *, public: bool = True, enabled: bool = True) -> _PoolRow:
    return _PoolRow.model_validate({
        "id": identifier, "name": identifier, "visibility": "public" if public else "restricted",
        "enabled": enabled, "source_kind": "anthropic", "updated_at": _NOW, "version": 1,
    })


def _deployment(identifier: str, model: str = "shared") -> dict[str, object]:
    return {"model_name": model, "model_info": {"id": identifier}, "litellm_params": {"model": "test/model"}}


@pytest.mark.parametrize("hours", tuple(ModelMetricHours))
def test_requested_window_has_exact_duration_and_bounded_utc_trend(hours: ModelMetricHours) -> None:
    window: Final = model_metrics_window(hours, _NOW)
    result: Final = metrics_page("model", "gateway", window, ())
    assert window.end - window.start == timedelta(hours=hours)
    assert 2 <= len(result.trend) <= 721
    assert result.trend[0].time <= window.start
    assert result.trend[-1].time <= window.end
    assert all(item.time.tzinfo == timezone.utc for item in result.trend)


@pytest.mark.parametrize("hours", [0, 1, 6, 48, 721, -1, "24;DROP TABLE logs"])
def test_only_supported_window_values_are_accepted(hours: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ModelMetricHours).validate_python(hours)


def test_public_statistics_never_include_private_draft_stale_or_other_model_deployments() -> None:
    pools: Final = (_pool("public"), _pool("private", public=False), _pool("draft", enabled=False))
    bindings: Final = (
        _StoredBinding(pool_id="public", deployment_id="allowed", model="shared"),
        _StoredBinding(pool_id="private", deployment_id="private", model="shared"),
        _StoredBinding(pool_id="draft", deployment_id="draft", model="shared"),
        _StoredBinding(pool_id="public", deployment_id="gone", model="shared"),
        _StoredBinding(pool_id="public", deployment_id="other", model="different"),
    )
    deployments: Final = tuple(_deployment(item.deployment_id, item.model) for item in bindings if item.deployment_id != "gone")
    assert selected_deployment_ids("shared", pools, bindings, deployments, None) == ("allowed",)


def test_public_listed_model_without_public_resources_has_no_measurements() -> None:
    pools: Final = (_pool("private", public=False),)
    bindings: Final = (_StoredBinding(pool_id="private", deployment_id="private", model="shared"),)
    assert selected_deployment_ids("shared", pools, bindings, (_deployment("private"),), None) == ()
    with pytest.raises(HTTPException) as error:
        selected_deployment_ids("not-listed", pools, bindings, (_deployment("private"),), None)
    assert error.value.status_code == 404


def test_personal_statistics_keep_account_pool_and_model_pairs_together() -> None:
    pools: Final = (_pool("one", public=False), _pool("two", public=False))
    bindings: Final = (
        _StoredBinding(pool_id="one", deployment_id="one", model="shared"),
        _StoredBinding(pool_id="two", deployment_id="two", model="shared"),
        _StoredBinding(pool_id="one", deployment_id="other", model="different"),
    )
    deployments: Final = tuple(_deployment(item.deployment_id, item.model) for item in bindings)
    access: Final = PoolAccess(mode="custom", grants=(PoolGrant(pool_id="one", models=("shared",)),))
    assert selected_deployment_ids("shared", pools, bindings, deployments, access) == ("one",)
    with pytest.raises(HTTPException) as error:
        selected_deployment_ids("different", pools, bindings, deployments, access)
    assert error.value.status_code == 404


def test_empty_statistics_have_zero_requests_without_invented_usage_latency_or_success() -> None:
    result: Final = metrics_page("example", "gateway", model_metrics_window(ModelMetricHours.DAY, _NOW), ())
    assert result.summary.requests == 0
    assert result.summary.success_rate_pct is None
    assert result.summary.total_tokens is None
    assert result.summary.ttft_p50_ms is None
    assert result.summary.output_tokens_per_second_p50 is None
    assert all(item.requests == 0 and item.success_rate_pct is None and item.ttft_p50_ms is None for item in result.trend)


def test_unknown_status_does_not_imply_success_and_recorded_zero_is_preserved() -> None:
    row: Final = _Aggregate(is_summary=True, requests=3, successful_requests=2, unknown_status_requests=1)
    assert summarize(row).success_rate_pct is None
    known: Final = summarize(row.model_copy(update={"unknown_status_requests": 0, "failed_requests": 1}))
    assert known.success_rate_pct == 100 * 2 / 3
    recorded: Final = summarize(_Aggregate(is_summary=True, requests=1, failed_requests=1, total_tokens=0, usage_samples=1))
    assert recorded.success_rate_pct == 0
    assert recorded.total_tokens == 0


def test_sparse_trend_preserves_observed_points_and_fills_only_empty_requests() -> None:
    window: Final = model_metrics_window(ModelMetricHours.DAY, _NOW)
    bucket: Final = _NOW.replace(minute=0)
    row: Final = _Aggregate(
        is_summary=False, bucket=bucket.replace(tzinfo=None), requests=2, successful_requests=1, failed_requests=1,
        ttft_samples=1, ttft_p50_ms=120, output_tokens_per_second_p50=20, throughput_samples=1,
    )
    result: Final = metrics_page("example", "account", window, (row, row.model_copy(update={"is_summary": True})))
    observed: Final = next(item for item in result.trend if item.time == bucket)
    assert observed.requests == result.summary.requests
    assert observed.success_rate_pct == 50
    assert observed.ttft_p50_ms == row.ttft_p50_ms
    assert sum(item.requests for item in result.trend) == result.summary.requests
    assert all(item.ttft_p50_ms is None for item in result.trend if item.time != bucket)


@pytest.mark.asyncio
async def test_cache_reuses_recent_observations_and_reloads_after_expiry() -> None:
    clock: Final = _Clock()
    cache: Final = _MetricsCache(clock)
    first: Final = metrics_page("example", "gateway", model_metrics_window(ModelMetricHours.DAY, _NOW), ())
    second: Final = first.model_copy(update={"window": model_metrics_window(ModelMetricHours.DAY, _NOW + timedelta(seconds=21))})
    responses: Final = iter((first, second))

    async def load() -> ModelMetricsPage:
        return next(responses)

    key: Final = ("database", None, "example", ModelMetricHours.DAY, ("deployment",))
    assert await cache.get(key, load) is first
    clock.value = 1
    assert await cache.get(key, load) is first
    clock.value = 21
    assert await cache.get(key, load) is second


@pytest.mark.asyncio
async def test_cache_does_not_cross_account_database_or_authorized_deployment_scope() -> None:
    cache: Final = _MetricsCache(_Clock())
    pages: Final = tuple(
        metrics_page("example", "account" if index else "gateway", model_metrics_window(ModelMetricHours.DAY, _NOW), ())
        for index in range(4)
    )
    responses: Final = iter(pages)

    async def load() -> ModelMetricsPage:
        return next(responses)

    keys: Final = (
        ("one", None, "example", ModelMetricHours.DAY, ("a",)),
        ("one", "alice", "example", ModelMetricHours.DAY, ("a",)),
        ("one", "alice", "example", ModelMetricHours.DAY, ("b",)),
        ("two", "alice", "example", ModelMetricHours.DAY, ("b",)),
    )
    for key, expected in zip(keys, pages, strict=True):
        assert await cache.get(key, load) is expected
