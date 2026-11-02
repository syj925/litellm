import json
from collections.abc import Callable, Mapping
from typing import Final

from litellm.litellm_core_utils.hidden_params import get_hidden_params
from litellm.proxy.common_utils.sse_keepalive import split_complete_sse_frames

from .expression_parser import REPORTED_ATTRIBUTE_PATHS
from .pricing import as_mapping

_TERMINAL_RESPONSES: Final = frozenset({"response.completed", "response.incomplete", "response.failed"})
_MAX_FRAME: Final = 4 * 1024 * 1024


class SettlementStreamFacts:
    def __init__(
        self,
        split_frames: Callable[[bytes], tuple[bytes, bytes]] = split_complete_sse_frames,
    ) -> None:
        self._usage: Mapping[str, object] = {}
        self._attributes: Mapping[str, object] = {}
        self._pending = bytearray()
        self._tail = b""
        self._reported = False
        self._terminal = False
        self._emitted = False
        self._overflow = False
        self._split_frames = split_frames

    def _payload(self, payload: Mapping[str, object]) -> None:
        raw_kind: Final = payload.get("type")
        kind: Final = raw_kind if isinstance(raw_kind, str) else None
        container: Final = (
            as_mapping(payload.get("message"))
            if kind == "message_start"
            else as_mapping(payload.get("response"))
            if kind in _TERMINAL_RESPONSES
            else payload
        )
        raw_usage: Final = as_mapping(container.get("usage") or container.get("usageMetadata"))
        usage: Final = (
            {key: value for key, value in raw_usage.items() if key != "output_tokens"}
            if kind == "message_start"
            else raw_usage
        )
        if usage:
            self._usage = {**self._usage, **usage}
            self._reported = True
        self._attributes = {
            **self._attributes,
            **{key: container[key] for key in REPORTED_ATTRIBUTE_PATHS if key in container},
        }
        if (
            kind in _TERMINAL_RESPONSES
            or kind == "message_stop"
            or kind == "message_delta"
            and as_mapping(payload.get("delta")).get("stop_reason") is not None
        ):
            self._terminal = True
        elif usage and "choices" in payload:
            choices: Final = payload.get("choices")
            complete: Final = any(key in self._usage for key in ("prompt_tokens", "input_tokens")) and any(
                key in self._usage for key in ("completion_tokens", "output_tokens")
            )
            if complete and (
                not choices
                or isinstance(choices, (list, tuple))
                and any(as_mapping(choice).get("finish_reason") is not None for choice in choices)
            ):
                self._terminal = True
        elif usage and "usageMetadata" in payload:
            candidates: Final = payload.get("candidates")
            if isinstance(candidates, (list, tuple)) and any(
                as_mapping(candidate).get("finishReason") is not None for candidate in candidates
            ):
                self._terminal = True

    def _frame(self, frame: bytes) -> None:
        if len(frame) > _MAX_FRAME:
            self._overflow = True
            return
        text: Final = frame.decode("utf-8", errors="replace")
        data: Final = "\n".join(line[5:].lstrip() for line in text.splitlines() if line.startswith("data:"))
        if data == "[DONE]":
            self._terminal = True
        elif any(marker in data for marker in ('"usage"', '"usageMetadata"', '"message_stop"', *_TERMINAL_RESPONSES)):
            try:
                parsed: Final = json.loads(data)
            except ValueError:
                self._overflow = True
            else:
                if isinstance(parsed, Mapping):
                    self._payload(parsed)

    def _wire(self, raw: bytes) -> None:
        complete, _ = self._split_frames(self._tail + raw)
        boundary: Final = len(self._pending) - len(self._tail) + len(complete) if complete else 0
        self._pending.extend(raw)
        if boundary:
            packet: Final = bytes(self._pending[:boundary])
            del self._pending[:boundary]
            for frame in packet.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n\n"):
                self._frame(frame)
        self._tail = bytes(self._pending[-3:])
        if len(self._pending) > _MAX_FRAME:
            self._overflow = True
            self._pending.clear()
            self._tail = b""

    def feed(self, chunk: object) -> Mapping[str, object] | None:
        if self._emitted:
            return None
        if isinstance(chunk, (bytes, str)):
            self._wire(chunk if isinstance(chunk, bytes) else chunk.encode())
        elif isinstance(chunk, Mapping):
            self._payload(chunk)
        elif getattr(chunk, "usage", None) is not None or getattr(chunk, "type", None) in _TERMINAL_RESPONSES:
            hidden: Final = get_hidden_params(chunk)
            reported: Final = hidden.get("retail_reported_usage") if hidden is not None else None
            self._payload(
                {**as_mapping(chunk), "usage": reported} if isinstance(reported, Mapping) else as_mapping(chunk)
            )
            if isinstance(reported, Mapping) and hidden is not None and hidden.get("retail_usage_final", True):
                self._terminal = True
        if self._terminal:
            self._emitted = True
            return self.snapshot()
        return None

    def snapshot(self) -> Mapping[str, object]:
        return {"usage": dict(self._usage), **self._attributes} if self._reported and not self._overflow else {}

    def finish(self) -> Mapping[str, object]:
        if self._pending:
            self._frame(bytes(self._pending))
            self._pending.clear()
            self._tail = b""
        return self.snapshot()

    @property
    def emitted(self) -> bool:
        return self._emitted
