from datetime import datetime, timezone
from typing import Final

import pytest

from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.proxy.retail.cache_observations import (
    CacheObservation,
    CacheObservationCollector,
    _CacheLogRow,
    cache_observation,
    cache_usage_view,
    enrich_log_payload,
    legacy_cache_usage,
)
from litellm.types.llms.openai import ResponsesAPIResponse

_START: Final = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)
_KWARGS: Final = {"litellm_call_id": "server-call", "litellm_params": {"metadata": {"user_api_key_user_id": "owner"}}}
_RESPONSE: Final = {
    "usage": {
        "input_tokens": 30, "output_tokens": 2,
        "input_tokens_details": {
            "cached_tokens": 3, "cache_write_tokens": 20,
            "cache_write_tokens_details": {"ttl_30m_tokens": 8, "ttl_12h_tokens": 12},
        },
    },
}


def test_observation_preserves_ttl_subsets_and_exact_native_millisecond_start() -> None:
    result: Final = cache_observation(_KWARGS, _RESPONSE, _START)
    assert result is not None
    assert result.started_at == datetime(2026, 1, 1, 0, 0, 0, 123000)
    assert result.cache_read_tokens == 3
    assert result.cache_write_tokens == 20
    assert result.cache_write_ttl_tokens == {"1800": 8, "43200": 12}
    assert result.cache_write_unclassified_tokens == 0


@pytest.mark.parametrize("response", (
    {}, {"usage": {}}, {"usage": {"input_tokens": -1, "output_tokens": 1}},
    {"usage": {"input_tokens": 10, "output_tokens": 1}},
))
def test_unusable_usage_produces_no_observation(response: object) -> None:
    assert cache_observation(_KWARGS, response, _START) is None


def test_legacy_write_total_is_unspecified_instead_of_assigned_a_guessed_duration() -> None:
    result: Final = cache_observation(
        _KWARGS, {"usage": {"input_tokens": 10, "output_tokens": 2, "cache_creation_input_tokens": 14}}, _START,
    )
    assert result is not None
    assert result.cache_write_tokens == 14
    assert result.cache_write_ttl_tokens == {}
    assert result.cache_write_unclassified_tokens == 14


@pytest.mark.asyncio
async def test_complete_stream_usage_wins_over_partial_object_and_writes_off_response_path() -> None:
    saved: list[CacheObservation] = []

    async def writer(value: CacheObservation) -> None:
        saved.append(value)

    collector: Final = CacheObservationCollector(writer)
    await collector.async_log_success_event(
        {**_KWARGS, "complete_streaming_response": _RESPONSE}, {}, _START, _START,
    )
    await collector.drain()
    assert len(saved) == 1
    assert saved[0].cache_write_ttl_tokens == {"1800": 8, "43200": 12}


@pytest.mark.asyncio
async def test_native_responses_callback_preserves_usage_and_authenticated_identity() -> None:
    saved: list[CacheObservation] = []

    async def writer(value: CacheObservation) -> None:
        saved.append(value)

    response: Final = ResponsesAPIResponse(
        id="resp_fixture", created_at=1780000000, model="fixture-model", output=[], object="response",
        usage={
            "input_tokens": 1300, "output_tokens": 10, "total_tokens": 1310,
            "input_tokens_details": {
                "cached_tokens": 200, "cache_write_tokens": 1000,
                "cache_write_tokens_details": {"ttl_30m_tokens": 400, "ttl_12h_tokens": 600},
            },
        },
    )
    collector: Final = CacheObservationCollector(writer)
    logging: Final = Logging(
        model="fixture-model", messages=[], stream=False, call_type="aresponses", start_time=_START,
        litellm_call_id="responses-call", function_id="fixture",
    )
    logging.model_call_details["litellm_params"] = {"litellm_metadata": {
        "user_api_key_user_id": "responses-owner",
    }}
    native_start, native_end, callback_response = logging._success_handler_helper_fn(
        result=response, start_time=_START, end_time=_START, standard_logging_object={"response": {}},
    )
    assert callback_response is not response
    assert callback_response.model_dump()["usage"]["input_tokens_details"]["cache_write_tokens"] == 1000
    normalized: Final = logging.model_call_details["standard_logging_object"]["response"]["usage"]
    assert normalized["prompt_tokens"] == 1300
    assert normalized["prompt_tokens_details"]["cache_write_tokens_details"]["ttl_30m_tokens"] == 400
    assert normalized["prompt_tokens_details"]["cache_write_tokens_details"]["ttl_12h_tokens"] == 600
    await collector.async_log_success_event(
        logging.model_call_details, callback_response, native_start, native_end,
    )
    await collector.drain()
    assert len(saved) == 1
    assert saved[0].litellm_call_id == "responses-call"
    assert saved[0].user_id == "responses-owner"
    assert saved[0].started_at == datetime(2026, 1, 1, 0, 0, 0, 123000)
    assert saved[0].cache_read_tokens == 200
    assert saved[0].cache_write_tokens == 1000
    assert saved[0].cache_write_ttl_tokens == {"1800": 400, "43200": 600}


def test_safe_usage_projection_rejects_inconsistent_subsets_and_removes_extra_data() -> None:
    value: Final = {
        "cache_read_tokens": 1, "cache_write_tokens": 10,
        "cache_write_ttl_tokens": {"300": 3, "3600": 5}, "cache_write_unclassified_tokens": 2,
    }
    assert cache_usage_view({**value, "api_key": "not-for-display"}) == value
    assert cache_usage_view({**value, "cache_write_unclassified_tokens": 3}) is None
    assert cache_usage_view({**value, "cache_write_ttl_tokens": {"invalid": 8}}) is None


def test_historical_native_cache_totals_are_available_to_customer_logs_without_private_metadata() -> None:
    result: Final = legacy_cache_usage({"metadata": {
        "api_base": "private", "additional_usage_values": {"cache_creation_input_tokens": 10, "cache_read_input_tokens": 2},
    }})
    assert result == {
        "cache_read_tokens": 2, "cache_write_tokens": 10, "cache_write_ttl_tokens": {},
        "cache_write_unclassified_tokens": 10,
    }


def test_log_enrichment_matches_start_exactly_and_refuses_ambiguous_request_id_reuse() -> None:
    observation: Final = cache_observation(_KWARGS, _RESPONSE, _START)
    assert observation is not None
    first: Final = _CacheLogRow(request_id="request", **observation.model_dump())
    second: Final = first.model_copy(update={"started_at": datetime(2026, 1, 2)})
    page: Final = {"data": [{"request_id": "request", "startTime": "2026-01-01T00:00:00.123Z"}], "total": 1}
    assert enrich_log_payload(page, (first, second)) == {
        **page, "data": [{**page["data"][0], "cache_usage": cache_usage_view(first)}],
    }
    assert enrich_log_payload({"request_id": "request"}, (first, second)) == {"request_id": "request"}
