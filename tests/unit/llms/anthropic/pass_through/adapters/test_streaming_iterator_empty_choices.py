"""
Regression tests for OpenAI-compatible chunks with an empty ``choices`` list.

``choices: []`` is valid OpenAI-compatible streaming: vLLM (and OpenAI itself,
when ``stream_options.include_usage`` is set) emits a final usage chunk with no
choices, and some gateways emit metadata-only chunks mid-stream. The adapter
used to index ``chunk.choices[0]`` unconditionally, so such a chunk raised
``IndexError: list index out of range`` and killed the ``/v1/messages`` stream.
"""

import asyncio
import json
from typing import Any, AsyncIterator, Dict, Final, List, Optional

import pytest

from litellm.llms.anthropic.pass_through.adapters.streaming_iterator import (
    AnthropicStreamWrapper,
)
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices, Usage


def _text_chunk(text: str) -> ModelResponseStream:
    return ModelResponseStream(choices=[StreamingChoices(index=0, delta=Delta(content=text), finish_reason=None)])


def _finish_chunk() -> ModelResponseStream:
    return ModelResponseStream(choices=[StreamingChoices(index=0, delta=Delta(), finish_reason="stop")])


def _empty_choices_chunk(usage: Optional[Usage] = None) -> ModelResponseStream:
    return ModelResponseStream(choices=[], usage=usage)


def _collect_async(wrapper: AnthropicStreamWrapper) -> str:
    async def _run() -> str:
        return "".join(
            [raw.decode() if isinstance(raw, bytes) else raw async for raw in wrapper.async_anthropic_sse_wrapper()]
        )

    return asyncio.run(_run())


def _message_delta(sse: str) -> Dict[str, Any]:
    return next(
        json.loads(line[len("data: ") :])
        for block in sse.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ") and '"message_delta"' in line
    )


def test_leading_metadata_chunk_without_choices_does_not_kill_stream():
    """A metadata-only chunk before any content must be skipped, not indexed."""
    chunks: List[ModelResponseStream] = [
        _empty_choices_chunk(),
        _text_chunk("Hello"),
        _text_chunk(" there"),
        _finish_chunk(),
    ]
    wrapper = AnthropicStreamWrapper(completion_stream=iter(chunks), model="mock-model")
    events = list(wrapper)

    text = "".join(event["delta"]["text"] for event in events if event.get("type") == "content_block_delta")
    assert text == "Hello there"
    assert events[-1]["type"] == "message_stop"


def test_final_usage_chunk_without_choices_is_merged_into_message_delta():
    """The vLLM/OpenAI final usage chunk carries no choices; its usage must
    still land on the Anthropic ``message_delta``."""
    usage = Usage(prompt_tokens=10, completion_tokens=3, total_tokens=13)

    async def _aiter() -> "AsyncIterator[ModelResponseStream]":
        for chunk in [_text_chunk("Hi"), _finish_chunk(), _empty_choices_chunk(usage)]:
            yield chunk

    sse = _collect_async(AnthropicStreamWrapper(completion_stream=_aiter(), model="mock-model"))

    message_delta = _message_delta(sse)
    assert message_delta["delta"]["stop_reason"] == "end_turn"
    assert message_delta["usage"]["input_tokens"] == 10
    assert message_delta["usage"]["output_tokens"] == 3
    assert "Hi" in sse
    assert "message_stop" in sse


@pytest.mark.parametrize("asynchronous", (False, True))
def test_streaming_pricing_attributes_survive_separate_metadata_stop_and_usage_chunks(asynchronous: bool) -> None:
    chunks: Final = (
        ModelResponseStream(choices=[], service_tier="fast"),
        _text_chunk("Hello"),
        _finish_chunk(),
        ModelResponseStream(
            choices=[], service_tier="default",
            usage=Usage(prompt_tokens=10, completion_tokens=3, inference_geo="us", speed="fast"),
        ),
    )

    async def stream() -> AsyncIterator[ModelResponseStream]:
        for chunk in chunks:
            yield chunk

    wrapper: Final = AnthropicStreamWrapper(
        completion_stream=stream() if asynchronous else iter(chunks), model="fixture",
    )
    usage: Final = (
        _message_delta(_collect_async(wrapper))["usage"] if asynchronous
        else next(event["usage"] for event in wrapper if event.get("type") == "message_delta")
    )
    assert usage == {
        "input_tokens": 10, "output_tokens": 3, "service_tier": "default", "inference_geo": "us", "speed": "fast",
    }
