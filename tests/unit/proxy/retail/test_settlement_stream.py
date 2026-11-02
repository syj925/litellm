import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Final

import pytest

from litellm.litellm_core_utils.hidden_params import set_hidden_param
from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from litellm.llms.anthropic.chat.handler import ModelResponseIterator as AnthropicIterator
from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import ModelResponseIterator as GeminiIterator
from litellm.proxy.common_utils.sse_keepalive import split_complete_sse_frames
from litellm.proxy.retail.pricing import usage_facts
from litellm.proxy.retail.settlement_store import fact_digest
from litellm.proxy.retail.settlement_stream import SettlementStreamFacts
from litellm.types.utils import ModelResponseStream, Usage


@pytest.mark.parametrize(
    "chunk",
    (
        {"choices": [{"delta": {"content": "private body"}}]},
        b'data: {"choices":[{"delta":{"content":"private body"}}]}\n\n',
    ),
)
def test_text_chunks_do_not_produce_billable_facts(chunk: object) -> None:
    facts: Final = SettlementStreamFacts()
    assert facts.feed(chunk) is None
    assert facts.finish() == {}


def test_chat_usage_is_captured_before_done_and_contains_no_text() -> None:
    facts: Final = SettlementStreamFacts()
    assert facts.feed({"choices": [{"delta": {"content": "secret"}}]}) is None
    final: Final = facts.feed(
        {
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            "service_tier": "priority",
            "api_key": "credential",
        }
    )
    assert final == {"usage": {"prompt_tokens": 10, "completion_tokens": 2}, "service_tier": "priority"}
    assert facts.feed("data: [DONE]\n\n") is None


def test_anthropic_split_usage_and_cache_ttls_are_combined_only_at_the_terminal_event() -> None:
    facts: Final = SettlementStreamFacts()
    assert (
        facts.feed(
            {
                "type": "message_start",
                "message": {
                    "content": [{"text": "private"}],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 4,
                        "cache_creation_input_tokens": 8,
                        "cache_creation": {"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 5},
                    },
                },
            }
        )
        is None
    )
    result: Final = facts.feed(
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}
    )
    normalized: Final = usage_facts(result)
    assert normalized is not None
    assert normalized.input_tokens == 10 and normalized.output_tokens == 2
    assert normalized.cache_read_tokens == 4 and normalized.cache_write_tokens == 8
    assert normalized.cache_write_ttl_tokens == {"300": 3, "3600": 5}
    assert "private" not in str(result)


@pytest.mark.parametrize("kind", ("response.completed", "response.incomplete", "response.failed"))
def test_responses_terminal_actual_usage_is_available_without_storing_output(kind: str) -> None:
    facts: Final = SettlementStreamFacts()
    result: Final = facts.feed(
        {
            "type": kind,
            "response": {
                "output": [{"text": "private"}],
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "service_tier": "default",
            },
        }
    )
    assert result == {"usage": {"input_tokens": 10, "output_tokens": 2}, "service_tier": "default"}


def test_fragmented_sse_is_not_committed_until_the_complete_terminal_payload_arrives() -> None:
    facts: Final = SettlementStreamFacts()
    wire: Final = (
        "event: response.completed\r\ndata: "
        + json.dumps(
            {
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 10, "output_tokens": 2}, "output": [{"text": "private"}]},
            }
        )
        + "\r\n\r\n"
    ).encode()
    outputs: Final = tuple(facts.feed(bytes([value])) for value in wire)
    assert all(value is None for value in outputs[:-1])
    assert outputs[-1] == {"usage": {"input_tokens": 10, "output_tokens": 2}}


def test_missing_usage_stays_unknown_instead_of_becoming_zero_usage() -> None:
    facts: Final = SettlementStreamFacts()
    assert facts.feed({"type": "response.completed", "response": {"output": []}}) == {}
    assert usage_facts(facts.snapshot()) is None


def test_fact_identity_excludes_optional_gateway_cost_but_preserves_usage_and_amount() -> None:
    facts: Final = {"usage": {"input_tokens": 10, "output_tokens": 2}}
    assert fact_digest(32000, facts) == fact_digest(32000, {**facts, "provider_cost": "0.4", "gateway_call_id": "call"})
    assert fact_digest(32000, facts) != fact_digest(32000, {"usage": {"input_tokens": 4, "output_tokens": 4}})
    assert fact_digest(32000, facts) != fact_digest(32001, facts)


@pytest.mark.parametrize(
    "reported",
    (
        {},
        {"prompt_tokens": 10},
        {"prompt_tokens": 0, "completion_tokens": 0},
        {"prompt_tokens": 10, "completion_tokens": 2},
    ),
)
def test_provider_reported_dimensions_override_assembler_estimates_without_inventing_missing_tokens(
    reported: Mapping[str, int],
) -> None:
    final: Final = ModelResponseStream(choices=[], usage=Usage(prompt_tokens=10, completion_tokens=5))
    set_hidden_param(final, "retail_reported_usage", reported)
    observed: Final = SettlementStreamFacts().feed(final)
    normalized: Final = usage_facts(observed)
    if "completion_tokens" not in reported:
        assert normalized is None
    else:
        assert normalized is not None
        assert normalized.input_tokens == reported["prompt_tokens"]
        assert normalized.output_tokens == reported["completion_tokens"]


def test_anthropic_initial_zero_output_does_not_claim_the_missing_terminal_output_is_zero() -> None:
    observed: Final = SettlementStreamFacts()
    assert (
        observed.feed({"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}}) is None
    )
    result: Final = observed.feed({"type": "message_stop"})
    assert result == {"usage": {"input_tokens": 10}}
    assert usage_facts(result) is None


class _ScanVolume:
    def __init__(self) -> None:
        self.total = 0

    def __call__(self, data: bytes) -> tuple[bytes, bytes]:
        self.total += len(data)
        return split_complete_sse_frames(data)


def test_fragmented_large_frame_scans_only_new_bytes_and_the_three_byte_delimiter_overlap() -> None:
    scan: Final = _ScanVolume()
    observed: Final = SettlementStreamFacts(scan)
    wire: Final = (
        b'data: {"choices":[{"delta":{"content":"'
        + b"x" * (1024 * 1024)
        + b'"}}]}\r\n\r\n'
        + b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}\r\n\r\n'
    )
    outputs: Final = tuple(observed.feed(wire[offset : offset + 97]) for offset in range(0, len(wire), 97))
    result: Final = next(value for value in reversed(outputs) if value is not None)
    assert result == {"usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    assert scan.total <= len(wire) + 3 * len(outputs)


@pytest.mark.parametrize("delimiter", (b"\r\r", b"\n\n", b"\r\n\r\n"))
def test_multiple_raw_sse_frames_accept_every_supported_line_ending(delimiter: bytes) -> None:
    observed: Final = SettlementStreamFacts()
    result: Final = observed.feed(
        b'data: {"choices":[{"delta":{"content":"private"}}]}'
        + delimiter
        + b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}'
        + delimiter
    )
    assert result == {"usage": {"prompt_tokens": 10, "completion_tokens": 2}}


def _logging() -> Logging:
    return Logging(
        model="gpt-6-astra",
        messages=[],
        stream=True,
        call_type="acompletion",
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        litellm_call_id="usage-fixture",
        function_id="fixture",
    )


@pytest.mark.parametrize("output", (None, 0, 2))
def test_anthropic_compatibility_adapter_preserves_raw_cache_fields_and_only_actual_final_output(
    output: int | None,
) -> None:
    provider: Final = AnthropicIterator(iter(()), sync_stream=True)
    start: Final = provider.chunk_parser(
        {
            "type": "message_start",
            "message": {
                "id": "msg-fixture",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 8,
                    "cache_creation": {"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 5},
                },
            },
        }
    )
    end: Final = provider.chunk_parser(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            **({"usage": {"output_tokens": output}} if output is not None else {}),
        }
    )
    wrapper: Final = CustomStreamWrapper(
        iter(()), model="gpt-6-astra", custom_llm_provider="openai", logging_obj=_logging()
    )
    wrapper._capture_reported_usage(start)
    wrapper._capture_reported_usage(end)
    final: Final = ModelResponseStream(choices=[], usage=Usage(prompt_tokens=22, completion_tokens=5))
    wrapper._stamp_reported_usage(final)
    normalized: Final = usage_facts(SettlementStreamFacts().feed(final))
    if output is None:
        assert normalized is None
    else:
        assert normalized is not None and normalized.output_tokens == output and normalized.input_tokens == 10
        assert normalized.cache_read_tokens == 4 and normalized.cache_write_tokens == 8
        assert normalized.cache_write_ttl_tokens == {"300": 3, "3600": 5}


@pytest.mark.parametrize(
    "metadata",
    (
        {"promptTokenCount": 10},
        {"promptTokenCount": 0, "candidatesTokenCount": 0},
        {"promptTokenCount": 12, "cachedContentTokenCount": 2, "candidatesTokenCount": 2},
    ),
)
def test_gemini_compatibility_adapter_does_not_promote_defaulted_zero_tokens_to_reported_usage(
    metadata: Mapping[str, int],
) -> None:
    provider: Final = GeminiIterator(iter(()), sync_stream=True, logging_obj=_logging())
    raw: Final = provider.chunk_parser(
        {
            "candidates": [],
            "usageMetadata": dict(metadata),
        }
    )
    wrapper: Final = CustomStreamWrapper(
        iter(()), model="gpt-6-astra", custom_llm_provider="openai", logging_obj=_logging()
    )
    wrapper._capture_reported_usage(raw)
    final: Final = ModelResponseStream(choices=[], usage=Usage(prompt_tokens=10, completion_tokens=5))
    wrapper._stamp_reported_usage(final)
    normalized: Final = usage_facts(SettlementStreamFacts().feed(final))
    if "candidatesTokenCount" not in metadata:
        assert normalized is None
    else:
        assert normalized is not None
        assert normalized.output_tokens == metadata["candidatesTokenCount"]
        assert normalized.input_tokens == metadata["promptTokenCount"] - metadata.get("cachedContentTokenCount", 0)
