import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Final

import pytest

from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.retail.model_observations import ModelObservation, ModelObservationCollector, output_signal
from litellm.types.utils import ModelResponseStream

_START: Final = datetime(2026, 1, 1, 1, 1, 1, 123456, tzinfo=timezone.utc)


def _request(call_id: str = "server-call") -> dict[str, object]:
    logging: Final = Logging(
        model="fixture", messages=[], stream=True, call_type="acompletion", start_time=_START,
        litellm_call_id=call_id, function_id="fixture", kwargs={},
    )
    return {"litellm_logging_obj": logging, "litellm_call_id": "untrusted-body-id", "start_time": "untrusted"}


async def _stream(chunks: Sequence[object]) -> AsyncGenerator[object, None]:
    for chunk in chunks:
        yield chunk


async def _success(collector: ModelObservationCollector, user: str, call_id: str = "server-call") -> None:
    await collector.async_log_success_event(
        {"litellm_call_id": call_id, "start_time": _START, "litellm_params": {"metadata": {"user_api_key_user_id": user}}},
        None, _START + timedelta(milliseconds=34), _START + timedelta(seconds=10),
    )


@pytest.mark.parametrize(
    ("payload", "protocol", "kind"),
    (
        ({"choices": [{"delta": {"content": " "}}]}, "openai_chat", "text"),
        ({"choices": [{"delta": {"reasoning_content": "thinking"}}]}, "openai_chat", "reasoning"),
        ({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "{"}}]}}]}, "openai_chat", "tool_arguments"),
        ({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}, "anthropic", "text"),
        ({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "thinking"}}, "anthropic", "reasoning"),
        ({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}}, "anthropic", "tool_arguments"),
        ({"type": "response.output_text.delta", "delta": "hello"}, "openai_responses", "text"),
        ({"type": "response.reasoning_text.delta", "delta": "thinking"}, "openai_responses", "reasoning"),
        ({"type": "response.function_call_arguments.delta", "delta": "{"}, "openai_responses", "tool_arguments"),
        ({"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}, "gemini", "text"),
        ({"candidates": [{"content": {"parts": [{"text": "thinking", "thought": True}]}}]}, "gemini", "reasoning"),
        ({"candidates": [{"content": {"parts": [{"functionCall": {"name": "tool", "args": {"x": 1}}}]}}]}, "gemini", "tool_arguments"),
    ),
)
def test_only_explicit_nonempty_model_output_is_classified(
    payload: Mapping[str, object], protocol: str, kind: str,
) -> None:
    signal: Final = output_signal(payload)
    assert signal is not None
    assert signal.protocol == protocol
    assert signal.kind == kind


@pytest.mark.parametrize(
    "payload",
    (
        {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"choices": [], "usage": {"completion_tokens": 10}},
        {"choices": [{"delta": {"tool_calls": [{"id": "tool", "function": {"name": "lookup"}}]}}]},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "delta": {"type": "signature_delta", "signature": "signed"}},
        {"type": "ping"},
        {"type": "response.created", "response": {"status": "in_progress"}},
        {"type": "response.output_text.delta", "delta": ""},
        {"type": {"unrecognized": "event"}, "delta": "text"},
        {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]},
        {"candidates": [{"content": {"parts": [{"inlineData": {"data": "audio", "mimeType": "audio/pcm"}}]}}]},
        {"error": {"message": "failed"}, "choices": [{"delta": {"content": "untrusted"}}]},
    ),
)
def test_start_heartbeat_usage_empty_metadata_and_unknown_outputs_are_not_tokens(payload: Mapping[str, object]) -> None:
    assert output_signal(payload) is None


@pytest.mark.asyncio
async def test_fragmented_sse_is_forwarded_unchanged_and_only_actual_output_sets_server_timing() -> None:
    records: Final[list[ModelObservation]] = []

    async def writer(record: ModelObservation) -> None:
        records.append(record)

    times: Final = iter((_START + timedelta(seconds=2), _START + timedelta(seconds=5)))
    collector: Final = ModelObservationCollector(writer, lambda: next(times))
    chunks: Final = (
        b': ping\n\n',
        b'event: message_start\ndata: {"type":"message_start"}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hel',
        b'lo"}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    )
    forwarded: Final = tuple([
        chunk async for chunk in collector.async_post_call_streaming_iterator_hook(
            UserAPIKeyAuth(user_id="authenticated-user"), _stream(chunks), _request(),
        )
    ])
    await _success(collector, "authenticated-user")
    await collector.drain()
    assert len(forwarded) == len(chunks)
    assert all(actual is original for actual, original in zip(forwarded, chunks, strict=True))
    assert len(records) == 1
    recorded: Final = records[0]
    assert recorded.first_output_at == (_START + timedelta(seconds=2)).replace(tzinfo=None)
    assert recorded.ended_at == (_START + timedelta(seconds=5)).replace(tzinfo=None)
    assert recorded.started_at.microsecond == (_START.microsecond + 34_000) // 1000 * 1000
    assert recorded.litellm_call_id == "server-call"
    assert recorded.user_id == "authenticated-user"
    assert "hello" not in recorded.model_dump_json()


@pytest.mark.asyncio
async def test_typed_chat_response_is_preserved_and_other_requests_do_not_share_timing() -> None:
    records: Final[list[ModelObservation]] = []

    async def writer(record: ModelObservation) -> None:
        records.append(record)

    times: Final = iter(_START + timedelta(seconds=value) for value in (2, 3, 6, 7))
    collector: Final = ModelObservationCollector(writer, lambda: next(times))
    content: Final = ModelResponseStream(choices=[{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}])
    first: Final = tuple([item async for item in collector.async_post_call_streaming_iterator_hook(
        UserAPIKeyAuth(user_id="one"), _stream((content,)), _request("call-one"),
    )])
    second: Final = tuple([item async for item in collector.async_post_call_streaming_iterator_hook(
        UserAPIKeyAuth(user_id="two"), _stream((content,)), _request("call-two"),
    )])
    await _success(collector, "one", "call-one")
    await _success(collector, "two", "call-two")
    await collector.drain()
    assert first[0] is content and second[0] is content
    assert {(record.user_id, record.litellm_call_id) for record in records} == {("one", "call-one"), ("two", "call-two")}
    assert records[0].first_output_at != records[1].first_output_at


@pytest.mark.asyncio
async def test_client_supplied_correlation_or_no_output_never_creates_a_measurement() -> None:
    records: Final[list[ModelObservation]] = []

    async def writer(record: ModelObservation) -> None:
        records.append(record)

    collector: Final = ModelObservationCollector(writer, lambda: _START)
    content: Final = {"choices": [{"delta": {"content": "hello"}}]}
    unsafe: Final = {"litellm_logging_obj": {"litellm_call_id": "forged", "start_time": _START}, "litellm_call_id": "forged"}
    for request, chunks in ((unsafe, (content,)), (_request(), (b': ping\n\n', b'data: [DONE]\n\n'))):
        async for item in collector.async_post_call_streaming_iterator_hook(UserAPIKeyAuth(user_id="one"), _stream(chunks), request):
            assert any(item is original for original in chunks)
    await collector.drain()
    assert not records


@pytest.mark.asyncio
async def test_cancellation_and_writer_failure_do_not_change_the_client_stream() -> None:
    records: Final[list[ModelObservation]] = []

    async def failed_writer(record: ModelObservation) -> None:
        records.append(record)
        raise RuntimeError("private database diagnostic")

    collector: Final = ModelObservationCollector(failed_writer, lambda: _START + timedelta(seconds=1))
    content: Final = {"choices": [{"delta": {"content": "hello"}}]}
    observed: Final = collector.async_post_call_streaming_iterator_hook(UserAPIKeyAuth(user_id="one"), _stream((content,)), _request())
    assert await observed.__anext__() is content
    await observed.aclose()
    await collector.drain()
    assert not records
    forwarded: Final = [chunk async for chunk in collector.async_post_call_streaming_iterator_hook(
        UserAPIKeyAuth(user_id="one"), _stream((content,)), _request(),
    )]
    await _success(collector, "one")
    await collector.drain()
    assert forwarded[0] is content
    assert len(records) == 1


@pytest.mark.asyncio
async def test_oversized_unterminated_sse_is_forwarded_but_not_timed() -> None:
    records: Final[list[ModelObservation]] = []

    async def writer(record: ModelObservation) -> None:
        records.append(record)

    collector: Final = ModelObservationCollector(writer, lambda: _START)
    oversized: Final = b'data: ' + b'x' * 262_144
    forwarded: Final = [chunk async for chunk in collector.async_post_call_streaming_iterator_hook(
        UserAPIKeyAuth(user_id="one"), _stream((oversized,)), _request(),
    )]
    await collector.drain()
    assert forwarded[0] is oversized
    assert not records


@pytest.mark.asyncio
async def test_native_success_before_iterator_completion_still_records_exact_native_start() -> None:
    records: Final[list[ModelObservation]] = []

    async def writer(record: ModelObservation) -> None:
        records.append(record)

    collector: Final = ModelObservationCollector(writer, lambda: _START + timedelta(seconds=1))
    content: Final = {"choices": [{"delta": {"content": "hello"}}]}

    async def stream() -> AsyncGenerator[object, None]:
        yield content
        await _success(collector, "one")
        yield {"choices": [], "usage": {"completion_tokens": 1}}

    forwarded: Final = [item async for item in collector.async_post_call_streaming_iterator_hook(
        UserAPIKeyAuth(user_id="one"), stream(), _request(),
    )]
    await collector.drain()
    assert forwarded[0] is content
    assert len(records) == 1
    assert records[0].started_at == (_START + timedelta(milliseconds=34)).replace(tzinfo=None, microsecond=157000)


@pytest.mark.asyncio
async def test_reused_call_id_and_start_time_cannot_cross_authenticated_users() -> None:
    records: Final[list[ModelObservation]] = []

    async def writer(record: ModelObservation) -> None:
        records.append(record)

    collector: Final = ModelObservationCollector(writer, lambda: _START + timedelta(seconds=1))
    content: Final = {"choices": [{"delta": {"content": "hello"}}]}
    for user in ("one", "two"):
        async for item in collector.async_post_call_streaming_iterator_hook(UserAPIKeyAuth(user_id=user), _stream((content,)), _request()):
            assert item is content
    await _success(collector, "one")
    await _success(collector, "two")
    await collector.drain()
    assert {record.user_id for record in records} == {"one", "two"}


@pytest.mark.asyncio
async def test_observation_writes_are_limited_to_two_active_database_operations() -> None:
    class Writer:
        def __init__(self) -> None:
            self.active = 0
            self.maximum = 0
            self.records: list[ModelObservation] = []
            self.full = asyncio.Event()
            self.release = asyncio.Event()

        async def __call__(self, record: ModelObservation) -> None:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == 2:
                self.full.set()
            await self.release.wait()
            self.records.append(record)
            self.active -= 1

    writer: Final = Writer()
    collector: Final = ModelObservationCollector(writer, lambda: _START + timedelta(seconds=1))
    content: Final = {"choices": [{"delta": {"content": "hello"}}]}
    for call_id in ("one", "two", "three", "four"):
        async for item in collector.async_post_call_streaming_iterator_hook(UserAPIKeyAuth(user_id="one"), _stream((content,)), _request(call_id)):
            assert item is content
        await _success(collector, "one", call_id)
    await writer.full.wait()
    await asyncio.sleep(0)
    assert writer.maximum == 2
    writer.release.set()
    await collector.drain()
    assert writer.maximum == 2
    assert len(writer.records) == 4
