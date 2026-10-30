import asyncio
from collections.abc import AsyncGenerator, AsyncIterable, Awaitable, Callable, Mapping, Sequence
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


def _chat_signal(payload: Mapping[str, object]) -> OutputSignal | None:
    for choice in _items(payload.get("choices")):
        delta: Final = as_mapping(as_mapping(choice).get("delta"))
        if _nonempty(delta.get("content")):
            return OutputSignal("openai_chat", "text")
        if _nonempty(delta.get("reasoning_content")) or _nonempty(delta.get("reasoning")):
            return OutputSignal("openai_chat", "reasoning")
        if _nonempty(as_mapping(delta.get("function_call")).get("arguments")):
            return OutputSignal("openai_chat", "tool_arguments")
        if any(
            _nonempty(as_mapping(as_mapping(tool).get("function")).get("arguments"))
            for tool in _items(delta.get("tool_calls"))
        ):
            return OutputSignal("openai_chat", "tool_arguments")
    return None


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


def _gemini_signal(payload: Mapping[str, object]) -> OutputSignal | None:
    for candidate in _items(payload.get("candidates")):
        content: Final = as_mapping(as_mapping(candidate).get("content"))
        for part in _items(content.get("parts")):
            data: Final = as_mapping(part)
            if _nonempty(data.get("text")):
                return OutputSignal("gemini", "reasoning" if data.get("thought") is True else "text")
            if as_mapping(as_mapping(data.get("functionCall")).get("args")):
                return OutputSignal("gemini", "tool_arguments")
    return None


def output_signal(payload: Mapping[str, object]) -> OutputSignal | None:
    if payload.get("error"):
        return None
    return _chat_signal(payload) or _anthropic_signal(payload) or _responses_signal(payload) or _gemini_signal(payload)


class _OutputDetector:
    def __init__(self) -> None:
        self._pending = b""
        self._decoder = SSEDecoder()
        self._disabled = False

    def observe(self, chunk: object) -> OutputSignal | None:
        if self._disabled:
            return None
        if isinstance(chunk, (Mapping, BaseModel)):
            try:
                return output_signal(as_mapping(chunk))
            except (ValueError, TypeError):
                self._disabled = True
                return None
        if not isinstance(chunk, (str, bytes)):
            return None
        raw: Final = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        if len(self._pending) + len(raw) > _MAX_PENDING_BYTES:
            self._disabled = True
            self._pending = b""
            return None
        complete, pending = split_complete_sse_frames(self._pending + raw)
        self._pending = pending
        try:
            return self._frames(complete.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError):
            self._disabled = True
            self._pending = b""
            return None

    def _frames(self, content: str) -> OutputSignal | None:
        for line in content.splitlines():
            event: Final = self._decoder.decode(line)
            if event is None or not event.data or event.data == "[DONE]":
                continue
            try:
                payload: Final = _PAYLOAD.validate_json(event.data)
            except ValidationError:
                continue
            signal: Final = output_signal(payload)
            if signal is not None:
                return signal
        return None


class _ObservedStream:
    def __init__(self, correlation: _Correlation, clock: Callable[[], datetime]) -> None:
        self._correlation = correlation
        self._clock = clock
        self._detector = _OutputDetector()
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
    async with store.transaction() as connection:
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
