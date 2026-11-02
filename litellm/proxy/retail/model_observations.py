import asyncio
import re
from collections.abc import AsyncGenerator, AsyncIterable, Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Literal

from openai._streaming import SSEDecoder
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import get_litellm_metadata_from_kwargs
from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.common_utils.sse_keepalive import split_complete_sse_frames

from .pricing import as_mapping
from .store import get_store

OutputProtocol = Literal["openai_chat", "openai_responses", "anthropic", "gemini"]
OutputKind = Literal["text", "reasoning", "tool_arguments"]
_MAX_PENDING_BYTES: Final = 262_144
_MAX_PENDING_WRITES: Final = 64
_PAYLOAD: Final = TypeAdapter(dict[str, JsonValue])
_JSON_MARKERS: Final = re.compile(rb'["\\{}\[\]]')


def _json_markers(data: bytes) -> Iterator[re.Match[bytes]]:
    return _JSON_MARKERS.finditer(data)


def _sse_boundary(data: bytes) -> int:
    complete, _ = split_complete_sse_frames(data)
    return len(complete)


def _parse_payload(data: str | bytes) -> Mapping[str, JsonValue]:
    return _PAYLOAD.validate_json(data)


class _JSONStructure:
    def __init__(self, markers: Callable[[bytes], Iterable[re.Match[bytes]]]) -> None:
        self._markers = markers
        self._closers = bytearray()
        self._string = False
        self._escaped = False
        self._complete = False
        self._invalid = False

    @property
    def ready(self) -> bool:
        return self._complete and not self._invalid

    def reject(self) -> None:
        self._invalid = True

    def feed(self, data: bytes) -> None:
        if self._invalid:
            return
        if self._complete:
            self._invalid = bool(data.strip())
            return
        escaped_at = -1 if self._escaped else None  # rebind-ok: track a backslash across consecutive marker positions
        for marker in self._markers(data):
            position: Final = marker.start()
            if escaped_at is not None:
                escaped: Final = position == escaped_at + 1
                escaped_at = None
                if escaped:
                    continue
            value: Final = marker[0]
            if value == b'"':
                self._string = not self._string
            elif self._string:
                if value == b"\\":
                    escaped_at = position
            elif value in {b"{", b"["}:
                self._closers.append(125 if value == b"{" else 93)
            elif not self._closers or value[0] != self._closers[-1]:
                self._invalid = True
                return
            else:
                self._closers.pop()
                if not self._closers:
                    self._complete = True
                    self._invalid = bool(data[marker.end():].strip())
                    break
        self._escaped = escaped_at == len(data) - 1


@dataclass(frozen=True, slots=True)
class OutputSignal:
    protocol: OutputProtocol
    kind: OutputKind


class ModelObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    litellm_call_id: str
    user_id: str
    started_at: datetime
    first_output_at: datetime
    ended_at: datetime
    protocol: OutputProtocol
    output_kind: OutputKind


@dataclass(frozen=True, slots=True)
class _Correlation:
    litellm_call_id: str
    user_id: str
    started_at: datetime


def _utc_naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _correlation(auth: UserAPIKeyAuth, request: Mapping[str, object]) -> _Correlation | None:
    logging: Final = request.get("litellm_logging_obj")
    user_id: Final = auth.user_id
    if not isinstance(logging, Logging) or not isinstance(user_id, str) or not 0 < len(user_id) <= 191:
        return None
    call_id: Final = logging.litellm_call_id
    if not isinstance(call_id, str) or not 0 < len(call_id) <= 191 or not isinstance(logging.start_time, datetime):
        return None
    started: Final = _utc_naive(logging.start_time)
    return _Correlation(call_id, user_id, started)


def _items(value: object) -> Sequence[object]:
    return value if isinstance(value, (list, tuple)) else ()


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _anthropic_signal(payload: Mapping[str, object]) -> OutputSignal | None:
    if payload.get("type") == "content_block_delta":
        delta: Final = as_mapping(payload.get("delta"))
        kind: Final = delta.get("type")
        if kind == "text_delta" and _nonempty(delta.get("text")):
            return OutputSignal("anthropic", "text")
        if kind == "thinking_delta" and _nonempty(delta.get("thinking")):
            return OutputSignal("anthropic", "reasoning")
        if kind == "input_json_delta" and _nonempty(delta.get("partial_json")):
            return OutputSignal("anthropic", "tool_arguments")
    if payload.get("type") == "content_block_start":
        block: Final = as_mapping(payload.get("content_block"))
        if block.get("type") == "text" and _nonempty(block.get("text")):
            return OutputSignal("anthropic", "text")
        if block.get("type") == "thinking" and _nonempty(block.get("thinking")):
            return OutputSignal("anthropic", "reasoning")
    return None


def _responses_signal(payload: Mapping[str, object]) -> OutputSignal | None:
    event_type: Final = payload.get("type")
    if not isinstance(event_type, str) or not _nonempty(payload.get("delta")):
        return None
    if event_type == "response.output_text.delta":
        return OutputSignal("openai_responses", "text")
    if event_type in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
        return OutputSignal("openai_responses", "reasoning")
    if event_type == "response.function_call_arguments.delta":
        return OutputSignal("openai_responses", "tool_arguments")
    return None


def output_signal(payload: Mapping[str, object]) -> OutputSignal | None:
    signals: Final = output_signals(payload)
    return signals[0] if signals else None


def _chat_output_signals(choice: object) -> tuple[OutputSignal, ...]:
    data: Final = as_mapping(choice)
    delta: Final = as_mapping(data.get("delta") or data.get("message"))
    text: Final = _nonempty(delta.get("content")) or _nonempty(data.get("text"))
    reasoning: Final = _nonempty(delta.get("reasoning_content")) or _nonempty(delta.get("reasoning"))
    tools: Final = _nonempty(as_mapping(delta.get("function_call")).get("arguments")) or any(
        _nonempty(as_mapping(as_mapping(tool).get("function")).get("arguments"))
        for tool in _items(delta.get("tool_calls"))
    )
    pairs: Final[tuple[tuple[OutputKind, bool], ...]] = (
        ("text", text), ("reasoning", reasoning), ("tool_arguments", tools),
    )
    return tuple(OutputSignal("openai_chat", kind) for kind, present in pairs if present)


def _anthropic_block_signals(block: object) -> tuple[OutputSignal, ...]:
    data: Final = as_mapping(block)
    kind: Final = data.get("type")
    if kind == "text" and _nonempty(data.get("text")):
        return (OutputSignal("anthropic", "text"),)
    if kind == "thinking" and _nonempty(data.get("thinking")):
        return (OutputSignal("anthropic", "reasoning"),)
    if kind == "tool_use" and as_mapping(data.get("input")):
        return (OutputSignal("anthropic", "tool_arguments"),)
    return ()


def _responses_item_signals(item: object) -> tuple[OutputSignal, ...]:
    data: Final = as_mapping(item)
    kind: Final = data.get("type")
    if not isinstance(kind, str):
        return ()
    if kind in {"function_call", "custom_tool_call"} and _nonempty(data.get("arguments") or data.get("input")):
        return (OutputSignal("openai_responses", "tool_arguments"),)
    parts: Final = _items(data.get("content")) if kind == "message" else _items(data.get("summary"))
    return tuple(
        OutputSignal("openai_responses", "reasoning" if kind == "reasoning" else "text")
        for part in parts if _nonempty(as_mapping(part).get("text"))
    )


def _gemini_part_signals(part: object) -> tuple[OutputSignal, ...]:
    data: Final = as_mapping(part)
    text: Final = (OutputSignal("gemini", "reasoning" if data.get("thought") is True else "text"),) if _nonempty(data.get("text")) else ()
    tools: Final = (OutputSignal("gemini", "tool_arguments"),) if as_mapping(as_mapping(data.get("functionCall")).get("args")) else ()
    return (*text, *tools)


def _gemini_candidate_signals(candidate: object) -> tuple[OutputSignal, ...]:
    content: Final = as_mapping(as_mapping(candidate).get("content"))
    return sum((_gemini_part_signals(part) for part in _items(content.get("parts"))), ())


def output_signals(payload: Mapping[str, object]) -> tuple[OutputSignal, ...]:
    if payload.get("error"):
        return ()
    native_anthropic: Final = _anthropic_signal(payload)
    native_responses: Final = _responses_signal(payload)
    anthropic: Final = as_mapping(payload.get("message")) if payload.get("type") == "message_start" else payload
    responses: Final = as_mapping(payload.get("response")) if payload.get("type") == "response.completed" else payload
    collected: Final = (
        *sum((_chat_output_signals(choice) for choice in _items(payload.get("choices"))), ()),
        *((native_anthropic,) if native_anthropic is not None else ()),
        *sum((_anthropic_block_signals(block) for block in _items(anthropic.get("content"))), ()),
        *((native_responses,) if native_responses is not None else ()),
        *sum((_responses_item_signals(item) for item in _items(responses.get("output"))), ()),
        *_responses_item_signals(payload.get("item")),
        *sum((_gemini_candidate_signals(candidate) for candidate in _items(payload.get("candidates"))), ()),
    )
    return tuple(dict.fromkeys(collected))


class OutputDetector:
    def __init__(
        self, *, _scan_sse: Callable[[bytes], int] = _sse_boundary,
        _scan_json: Callable[[bytes], Iterable[re.Match[bytes]]] = _json_markers,
        _parse: Callable[[str | bytes], Mapping[str, JsonValue]] = _parse_payload,
    ) -> None:
        self._pending = bytearray()
        self._tail = b""
        self._mode: Literal["json", "sse"] | None = None
        self._json: _JSONStructure | None = None
        self._scan_sse = _scan_sse
        self._scan_json = _scan_json
        self._parse = _parse
        self._decoder = SSEDecoder()
        self._disabled = False
        self.has_event = False
        self.terminal_event: str | None = None

    def observe(self, chunk: object) -> OutputSignal | None:
        signals: Final = self.observe_signals(chunk)
        return signals[0] if signals else None

    def observe_signals(self, chunk: object) -> tuple[OutputSignal, ...]:
        if self._disabled:
            return ()
        if isinstance(chunk, (Mapping, BaseModel)):
            try:
                self.has_event = True
                document: Final = as_mapping(chunk)
                self._observe_terminal(document)
                return output_signals(document)
            except (ValueError, TypeError):
                self._disabled = True
                return ()
        if not isinstance(chunk, (str, bytes)):
            return ()
        raw: Final = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        if len(self._pending) + len(raw) > _MAX_PENDING_BYTES:
            self._disable()
            return ()
        if self._mode is None:
            self._select_mode(raw)
        if self._mode == "json":
            return self._json_chunk(raw)
        return self._sse_chunk(raw)

    def _disable(self) -> None:
        self._disabled = True
        self._pending.clear()
        self._tail = b""
        self._json = None

    def _select_mode(self, raw: bytes) -> None:
        prefix: Final = raw.lstrip()
        self._mode = "json" if prefix.startswith(b"{") else "sse" if prefix else None

    def _json_chunk(self, raw: bytes) -> tuple[OutputSignal, ...]:
        payload: Final[Mapping[str, JsonValue]]
        if self._json is None:
            combined: Final = bytes(self._pending) + raw if self._pending else raw
            try:
                payload = self._parse(combined)
            except ValidationError:
                self._json = _JSONStructure(self._scan_json)
                self._json.feed(combined)
                if self._json.ready:
                    self._json.reject()
                self._pending.extend(raw)
                return ()
        else:
            self._pending.extend(raw)
            self._json.feed(raw)
            if not self._json.ready:
                return ()
            try:
                payload = self._parse(bytes(self._pending))
            except ValidationError:
                self._json.reject()
                return ()
        self._pending.clear()
        self._mode = None
        self._json = None
        self._tail = b""
        self.has_event = True
        self._observe_terminal(payload)
        return output_signals(payload)

    def _sse_chunk(self, raw: bytes) -> tuple[OutputSignal, ...]:
        window: Final = self._tail + raw
        boundary: Final = self._scan_sse(window)
        if boundary == 0:
            self._pending.extend(raw)
            self._tail = window[-3:]
            return ()
        end: Final = boundary - len(self._tail)
        complete: Final[bytes]
        if self._pending:
            self._pending.extend(raw[:end])
            complete = bytes(self._pending)
            self._pending.clear()
        else:
            complete = raw[:end]
        self._pending.extend(raw[end:])
        self._tail = bytes(self._pending[-3:])
        self._mode = None
        self._select_mode(bytes(self._pending))
        try:
            return self._frames(complete.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError):
            self._disable()
            return ()

    def _frames(self, content: str) -> tuple[OutputSignal, ...]:
        return tuple(dict.fromkeys(sum((self._line(line) for line in content.splitlines()), ())))

    def _line(self, line: str) -> tuple[OutputSignal, ...]:
        event: Final = self._decoder.decode(line)
        if event is None or not event.data:
            return ()
        self.has_event = True
        if event.data == "[DONE]":
            self.terminal_event = "[DONE]"
            return ()
        try:
            payload: Final = self._parse(event.data)
        except ValidationError:
            return ()
        self._observe_terminal(payload)
        return output_signals(payload)

    def _observe_terminal(self, payload: Mapping[str, object]) -> None:
        kind: Final = payload.get("type")
        if isinstance(kind, str) and kind in {"message_stop", "response.completed"}:
            self.terminal_event = kind


class _ObservedStream:
    def __init__(self, correlation: _Correlation, clock: Callable[[], datetime]) -> None:
        self._correlation = correlation
        self._clock = clock
        self._detector = OutputDetector()
        self._first_output_at: datetime | None = None
        self._signal: OutputSignal | None = None
        self._ended_at: datetime | None = None
        self._native_started_at: datetime | None = None

    def observe(self, chunk: object) -> None:
        if self._first_output_at is not None:
            return
        signal: Final = self._detector.observe(chunk)
        if signal is not None:
            self._first_output_at = _utc_naive(self._clock())
            self._signal = signal

    @property
    def has_output(self) -> bool:
        return self._first_output_at is not None

    def complete(self) -> None:
        self._ended_at = _utc_naive(self._clock())

    def set_native_start(self, value: datetime) -> None:
        started: Final = _utc_naive(value)
        self._native_started_at = started.replace(microsecond=started.microsecond // 1000 * 1000)

    def observation(self) -> ModelObservation | None:
        if self._first_output_at is None or self._signal is None or self._ended_at is None:
            return None
        if self._native_started_at is None or not self._native_started_at <= self._first_output_at <= self._ended_at:
            return None
        return ModelObservation(
            litellm_call_id=self._correlation.litellm_call_id, user_id=self._correlation.user_id,
            started_at=self._native_started_at, first_output_at=self._first_output_at, ended_at=self._ended_at,
            protocol=self._signal.protocol, output_kind=self._signal.kind,
        )


async def persist_observation(observation: ModelObservation) -> None:
    store: Final = get_store()
    await store.initialize()
    async with store.diagnostic_transaction() as connection:
        await connection.execute(
            """INSERT INTO retail_model_observations
            (litellm_call_id,user_id,started_at,first_output_at,ended_at,protocol,output_kind)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (litellm_call_id,user_id,started_at) DO UPDATE SET
            first_output_at=LEAST(retail_model_observations.first_output_at,EXCLUDED.first_output_at),
            ended_at=GREATEST(retail_model_observations.ended_at,EXCLUDED.ended_at)""",
            (
                observation.litellm_call_id, observation.user_id, observation.started_at,
                observation.first_output_at, observation.ended_at, observation.protocol, observation.output_kind,
            ),
        )


class ModelObservationCollector(CustomLogger):
    def __init__(
        self, writer: Callable[[ModelObservation], Awaitable[None]] = persist_observation,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        super().__init__()
        self._writer = writer
        self._clock = clock
        self._pending: set[asyncio.Task[None]] = set()
        self._active: dict[tuple[str, str, datetime], _ObservedStream] = {}
        self._write_slots = asyncio.Semaphore(2)

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: UserAPIKeyAuth, response: AsyncIterable[object], request_data: Mapping[str, object],
    ) -> AsyncGenerator[object, None]:
        correlation: Final = _correlation(user_api_key_dict, request_data)
        if correlation is None:
            async for chunk in response:
                yield chunk
            return
        stream: Final = _ObservedStream(correlation, self._clock)
        key: Final = (correlation.litellm_call_id, correlation.user_id, correlation.started_at)
        if len(self._active) >= _MAX_PENDING_WRITES:
            self._active.pop(next(iter(self._active)))
        self._active[key] = stream
        try:
            async for chunk in response:
                stream.observe(chunk)
                yield chunk
        except BaseException:
            self._active.pop(key, None)
            raise
        stream.complete()
        if not stream.has_output:
            self._active.pop(key, None)
            return
        self._flush(key, stream)

    async def async_log_success_event(
        self, kwargs: Mapping[str, object], response_obj: object, start_time: datetime, end_time: datetime,
    ) -> None:
        call_id: Final = kwargs.get("litellm_call_id")
        initial_start: Final = kwargs.get("start_time")
        metadata: Final = as_mapping(get_litellm_metadata_from_kwargs(dict(kwargs)))
        user_id: Final = metadata.get("user_api_key_user_id")
        if not isinstance(call_id, str) or not isinstance(initial_start, datetime) or not isinstance(start_time, datetime):
            return
        if not isinstance(user_id, str):
            return
        key: Final = (call_id, user_id, _utc_naive(initial_start))
        stream: Final = self._active.get(key)
        if stream is None:
            return
        stream.set_native_start(start_time)
        self._flush(key, stream)

    def _flush(self, key: tuple[str, str, datetime], stream: _ObservedStream) -> None:
        observation: Final = stream.observation()
        if observation is not None and len(self._pending) < _MAX_PENDING_WRITES:
            self._active.pop(key, None)
            task: Final = asyncio.create_task(self._write_safely(observation))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _write_safely(self, observation: ModelObservation) -> None:
        try:
            await asyncio.wait_for(self._bounded_write(observation), timeout=2)
        except Exception as exc:
            verbose_proxy_logger.warning("Model observation could not be saved (%s)", type(exc).__name__)

    async def _bounded_write(self, observation: ModelObservation) -> None:
        async with self._write_slots:
            await self._writer(observation)

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)


model_observation_callback: Final = ModelObservationCollector()
