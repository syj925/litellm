import asyncio
import json
import re
from collections.abc import AsyncGenerator, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Final

import pytest
from pydantic import JsonValue

from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.retail.model_observations import (
    _MAX_PENDING_BYTES,
    ModelObservation,
    ModelObservationCollector,
    OutputDetector,
    _json_markers,
    _parse_payload,
    _sse_boundary,
    output_signal,
)
from litellm.types.utils import ModelResponseStream


@pytest.mark.parametrize("payload", (
    {"type": {}}, {"type": []}, {"output": [{"type": {}}]}, {"output": [{"type": []}]},
))
def test_unrecognized_json_type_fields_do_not_interrupt_the_response(payload: Mapping[str, object]) -> None:
    detector: Final = OutputDetector()
    assert detector.observe_signals(json.dumps(payload).encode()) == ()
    assert detector.terminal_event is None

_START: Final = datetime(2026, 1, 1, 1, 1, 1, 123456, tzinfo=timezone.utc)


def test_detector_distinguishes_early_metadata_from_every_output_kind_in_the_same_network_chunk() -> None:
    detector: Final = OutputDetector()
    assert detector.observe_signals(b": keepalive\n\n") == () and not detector.has_event
    assert detector.observe_signals(b'data: {"type":"message_start","message":{"role":"assistant"}}\n\n') == ()
    assert detector.has_event
    signals: Final = detector.observe_signals(
        b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"private"}}\n\n'
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"visible"}}\n\n'
        b'data: {"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"{"}}\n\n'
    )
    assert tuple(signal.kind for signal in signals) == ("reasoning", "text", "tool_arguments")


def test_detector_parses_fragmented_nonstream_json_and_preserves_multiple_kinds_in_one_payload() -> None:
    detector: Final = OutputDetector()
    assert detector.observe_signals(b'{"choices":[{"message":') == () and not detector.has_event
    signals: Final = detector.observe_signals(b'{"content":"text","reasoning_content":"thought","tool_calls":'
                                             b'[{"function":{"arguments":"{}"}}]}}]}')
    assert detector.has_event
    assert {signal.kind for signal in signals} == {"text", "reasoning", "tool_arguments"}


@pytest.mark.parametrize("payload", (
    {"type": "message", "content": [{"type": "thinking", "thinking": "thought"}, {"type": "text", "text": "visible"}]},
    {"object": "response", "output": [{"type": "reasoning", "summary": [{"text": "thought"}]},
                                        {"type": "message", "content": [{"type": "output_text", "text": "visible"}]}]},
    {"candidates": [{"content": {"parts": [{"thought": True, "text": "thought"}, {"text": "visible"}]}}]},
))
def test_detector_keeps_reasoning_and_text_from_completed_native_responses(payload: dict[str, object]) -> None:
    assert {signal.kind for signal in OutputDetector().observe_signals(payload)} == {"reasoning", "text"}


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

class _DetectorWork:
    def __init__(self) -> None:
        self.json_bytes = 0
        self.sse_bytes = 0
        self.sse_calls = 0
        self.validated_bytes = 0
        self.validations = 0
        self.last_validated: str | bytes | None = None

    def json(self, data: bytes) -> Iterator[re.Match[bytes]]:
        self.json_bytes += len(data)
        return _json_markers(data)

    def sse(self, data: bytes) -> int:
        self.sse_bytes += len(data)
        self.sse_calls += 1
        return _sse_boundary(data)

    def parse(self, data: str | bytes) -> Mapping[str, JsonValue]:
        self.validated_bytes += len(data)
        self.validations += 1
        self.last_validated = data
        return _parse_payload(data)


def _counted_detector(work: _DetectorWork) -> OutputDetector:
    return OutputDetector(_scan_sse=work.sse, _scan_json=work.json, _parse=work.parse)


@pytest.mark.parametrize("size", (1, 2, 3, 7, 1024))
def test_fragmented_json_scans_new_bytes_and_validates_only_a_structurally_complete_document(size: int) -> None:
    work: Final = _DetectorWork()
    detector: Final = _counted_detector(work)
    body: Final = json.dumps({
        "type": "response.completed", "response": {"output": [
            {"type": "message", "content": [{"text": '你好 } ] \\" escaped " brackets { [' * 100}]},
            {"type": "reasoning", "summary": [{"text": "thought"}]},
            {"type": "function_call", "arguments": '{"quoted":"}\\\\\""}'},
        ]},
    }, ensure_ascii=False).encode("utf-8")
    chunks: Final = tuple(body[offset : offset + size] for offset in range(0, len(body), size))
    for chunk in chunks[:-1]:
        assert detector.observe_signals(chunk) == ()
        assert not detector.has_event and detector.terminal_event is None
    signals: Final = detector.observe_signals(chunks[-1])
    assert {signal.kind for signal in signals} == {"text", "reasoning", "tool_arguments"}
    assert detector.has_event and detector.terminal_event == "response.completed"
    assert work.json_bytes == len(body)
    assert work.validations == 2 and work.validated_bytes <= len(body) + size
    assert work.sse_bytes == 0


@pytest.mark.parametrize("size", (1, 2, 3, 7, 1024))
def test_fragmented_sse_scans_only_new_input_and_keeps_unicode_crlf_and_terminal_semantics(size: int) -> None:
    work: Final = _DetectorWork()
    detector: Final = _counted_detector(work)
    frames: Final = (
        b': ping\r\n\r\n',
        b'data: {"type":"message_start","message":{"role":"assistant"}}\r\n\r\n',
        ('data: ' + json.dumps({"type": "content_block_delta", "delta": {
            "type": "thinking_delta", "thinking": "思考" * 1000,
        }}, ensure_ascii=False) + '\r\n\r\n').encode(),
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"visible"}}\n\n',
        b'data: {"type":"message_stop"}\r\r',
        b'data: [DONE]\r\n\r\n',
    )
    wire: Final = b"".join(frames)
    signals: Final = sum((
        detector.observe_signals(wire[offset : offset + size]) for offset in range(0, len(wire), size)
    ), ())
    assert tuple(signal.kind for signal in signals) == ("reasoning", "text")
    assert detector.has_event and detector.terminal_event == "[DONE]"
    assert work.sse_bytes <= len(wire) + 3 * work.sse_calls
    assert work.json_bytes == 0 and work.validations == 4


def test_complete_json_chunk_keeps_the_original_bytes_fast_path_without_structure_scanning() -> None:
    work: Final = _DetectorWork()
    detector: Final = _counted_detector(work)
    body: Final = b'{"choices":[{"message":{"content":"visible"}}]}'
    assert detector.observe_signals(body)[0].kind == "text"
    assert work.last_validated is body and work.validations == 1
    assert work.json_bytes == 0 and work.sse_bytes == 0


@pytest.mark.parametrize("prefix", (b'{"choices":[{"message":{"content":"', b'data: {"choices":'))
def test_incremental_limit_disables_only_observation_and_never_accepts_later_terminal_events(prefix: bytes) -> None:
    detector: Final = OutputDetector()
    assert detector.observe_signals(prefix) == ()
    assert detector.observe_signals(b"x" * (_MAX_PENDING_BYTES - len(prefix))) == ()
    assert detector.observe_signals(b"x") == ()
    assert detector.observe_signals(b'data: {"type":"message_stop"}\n\n') == ()
    assert detector.observe_signals({"choices": [{"delta": {"content": "visible"}}]}) == ()
    assert not detector.has_event and detector.terminal_event is None


def test_complete_document_at_the_byte_limit_is_observed_without_relaxing_the_original_limit() -> None:
    body: Final = b'{"choices":[{"message":{"content":"visible"}}]}'
    at_limit: Final = body + b" " * (_MAX_PENDING_BYTES - len(body))
    assert OutputDetector().observe_signals(at_limit)[0].kind == "text"
    assert OutputDetector().observe_signals(at_limit + b" ") == ()


def test_finished_but_invalid_json_is_not_revalidated_for_every_later_fragment() -> None:
    work: Final = _DetectorWork()
    detector: Final = _counted_detector(work)
    assert detector.observe_signals(b'{"choices":') == ()
    assert detector.observe_signals(b'[]} trailing') == ()
    for _ in range(10):
        assert detector.observe_signals(b" ") == ()
    assert not detector.has_event and detector.terminal_event is None
    assert work.validations == 1
