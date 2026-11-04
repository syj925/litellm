import json
import tracemalloc
from collections.abc import Iterator, Mapping
from typing import Final

import pytest

from litellm.proxy._types import ProxyException
from litellm.proxy.common_request_processing import proxy_exception_from_route_error
from litellm.proxy.retail.customer_response_privacy import (
    CustomerSSEPrivacy,
    _frame_boundaries,
    customer_error_body,
    customer_error_payload,
    customer_response_headers,
    customer_response_payload,
)

_REQUEST_ID: Final = "gateway-request-123"
_SECRET: Final = "sk-fixture-private-value"
_DETAIL: Final = "deployment private-channel failed at https://private.invalid/v1; " + _SECRET


@pytest.mark.parametrize("wire_status", (200, 429))
def test_structured_model_budget_denial_keeps_a_fixed_public_action_and_machine_code(wire_status: int) -> None:
    denied: Final = ProxyException(message=_DETAIL, type="budget_exceeded", param="model", code=429)
    assert proxy_exception_from_route_error(denied) is denied
    result: Final = _mapping(
        customer_error_payload({"error": denied.to_dict()}, status_code=wire_status, request_id=_REQUEST_ID)
    )
    error: Final = _mapping(result["error"])
    assert error == {
        "message": "Your account budget for this model cannot cover this request. Reduce the request size or contact "
        "the account administrator to review the budget window.",
        "type": "budget_exceeded",
        "code": "budget_exceeded",
        "param": "model",
        "request_id": _REQUEST_ID,
    }
    assert _SECRET not in json.dumps(result) and "Retry later" not in str(error["message"])


def test_an_upstream_claiming_the_budget_type_cannot_reflect_its_message_or_private_fields() -> None:
    payload: Final = {
        "error": {
            "type": "budget_exceeded",
            "param": "model",
            "code": _SECRET,
            "message": _DETAIL,
            "provider_cost": "private-cost",
            "provider_specific_fields": {"api_key": _SECRET},
        },
        "api_base": "https://private.invalid",
        "upstream_request_id": _SECRET,
    }
    result: Final = _mapping(customer_error_payload(payload, status_code=429, request_id=_REQUEST_ID))
    error: Final = _mapping(result["error"])
    assert (error["type"], error["code"], error["param"]) == ("budget_exceeded", "budget_exceeded", "model")
    assert "budget window" in str(error["message"])
    assert all(value not in json.dumps(result) for value in (_SECRET, "private.invalid", "private-cost", "api_base"))


@pytest.mark.parametrize(
    ("kind", "param"),
    (("rate_limit_error", "model"), ("PrivateBudgetExceeded", "model"), ("budget_exceeded", "deployment_id")),
)
def test_other_429_errors_keep_existing_redaction_even_when_messages_mention_budgets(kind: str, param: str) -> None:
    result: Final = _mapping(
        customer_error_payload(
            {"error": {"type": kind, "param": param, "code": "429", "message": "budget exceeded " + _DETAIL}},
            status_code=429,
        )
    )
    error: Final = _mapping(result["error"])
    assert error["message"] == "The request limit has been reached. Retry later or reduce concurrent requests."
    assert error["code"] == "429" and _SECRET not in json.dumps(result)


def test_existing_402_wallet_budget_classification_does_not_become_a_model_window_denial() -> None:
    result: Final = _mapping(
        customer_error_payload(
            {"error": {"type": "budget_exceeded", "param": "model", "code": "402", "message": _DETAIL}}, status_code=402
        )
    )
    error: Final = _mapping(result["error"])
    assert error["code"] == "402" and error["type"] == "budget_exceeded"
    assert (
        error["message"] == "Insufficient wallet balance for this request. Add funds or lower the output token limit."
    )


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


@pytest.mark.parametrize("status", (400, 401, 403, 429, 500, 502, 503, 504))
def test_error_keeps_status_and_gateway_request_id_without_provider_fields(status: int) -> None:
    payload: Final = {
        "error": {
            "message": _DETAIL,
            "type": "PrivateSupplierError",
            "param": "deployment_id",
            "code": f"private-{_SECRET}",
            "provider_specific_fields": {"headers": {"x-request-id": "upstream"}},
        },
        "upstream_request_id": "upstream",
        "request_id": "upstream-id",
        "internal_debug": _SECRET,
    }
    result: Final = _mapping(customer_error_payload(payload, status_code=status, request_id=_REQUEST_ID))
    error: Final = _mapping(result["error"])
    assert error["code"] == str(status) and error["request_id"] == result["request_id"] == _REQUEST_ID
    assert error["param"] is None and error["message"]
    serialized: Final = json.dumps(result)
    assert all(private not in serialized for private in (_SECRET, "private.invalid", "private-channel", "upstream-id"))
    assert "provider_specific_fields" not in error


@pytest.mark.parametrize(
    ("status", "message", "param", "code"),
    (
        (402, "Insufficient wallet balance. Add funds or lower the output token limit", None, "insufficient_balance"),
        (429, "Your wallet has reached its concurrent request limit", None, "rate_limit_exceeded"),
        (
            403,
            "The requested model 'public-model' is not available for this API key, or the model name is invalid",
            "model",
            "model_not_allowed",
        ),
        (400, "max_tokens must be an integer between 1 and 1000000", "max_tokens", "invalid_parameter"),
        (422, "messages[0].content is required", "messages[0].content", "missing_required_parameter"),
    ),
)
def test_customer_correctable_error_keeps_safe_specific_diagnostics(
    status: int,
    message: str,
    param: str | None,
    code: str,
) -> None:
    result: Final = _mapping(
        customer_error_payload(
            {
                "error": {"message": message, "type": "invalid_request_error", "param": param, "code": code},
            },
            status_code=status,
            request_id=_REQUEST_ID,
        )
    )
    error: Final = _mapping(result["error"])
    assert error["message"] == message and error["param"] == param and error["code"] == code


@pytest.mark.parametrize(
    "detail",
    (
        "Insufficient wallet balance. Add funds or lower the output token limit",
        {"error": "Insufficient wallet balance. Add funds or lower the output token limit"},
        {"error": {"message": "Insufficient wallet balance. Add funds or lower the output token limit"}},
    ),
)
def test_fastapi_detail_shapes_preserve_wallet_action(detail: object) -> None:
    result: Final = _mapping(customer_error_payload({"detail": detail}, status_code=402))
    assert (
        _mapping(result["error"])["message"] == "Insufficient wallet balance. Add funds or lower the output token limit"
    )


@pytest.mark.parametrize("kind", ("type", "code", "param", "message"))
def test_malformed_error_fields_do_not_throw_or_echo_private_values(kind: str) -> None:
    result: Final = customer_error_payload({"error": {kind: {_SECRET: [_DETAIL]}}}, status_code=503)
    assert _SECRET not in json.dumps(result) and result is not None


def test_anthropic_gemini_and_responses_errors_keep_their_protocol_envelopes() -> None:
    anthropic: Final = _mapping(
        customer_error_payload(
            {
                "type": "error",
                "error": {"type": "overloaded_error", "message": _DETAIL},
                "request_id": "upstream",
            },
            status_code=200,
            request_id=_REQUEST_ID,
        )
    )
    assert anthropic["type"] == "error" and _mapping(anthropic["error"])["code"] == "503"
    gemini: Final = _mapping(
        customer_error_payload(
            {
                "error": {"code": 503, "status": "UNAVAILABLE", "message": _DETAIL, "details": [{"metadata": _SECRET}]},
            },
            status_code=200,
        )
    )
    assert _mapping(gemini["error"])["code"] == 503 and _mapping(gemini["error"])["status"] == "UNAVAILABLE"
    output: Final = [{"type": "message", "content": [{"type": "output_text", "text": _DETAIL}]}]
    responses: Final = _mapping(
        customer_error_payload(
            {
                "type": "response.failed",
                "sequence_number": 7,
                "response": {
                    "id": "resp_public",
                    "model": "public-model",
                    "status": "failed",
                    "output": output,
                    "error": {"code": "server_error", "message": _DETAIL},
                    "provider_specific_fields": {_SECRET: _DETAIL},
                },
            },
            status_code=200,
            request_id=_REQUEST_ID,
        )
    )
    assert responses["type"] == "response.failed" and responses["sequence_number"] == 7
    response: Final = _mapping(responses["response"])
    assert response["output"] == output and response["model"] == "public-model"
    assert _SECRET not in json.dumps(response["error"]) and "provider_specific_fields" not in response


def test_safe_headers_keep_wire_contract_and_own_id_without_internal_headers_or_arbitrary_rate_text() -> None:
    headers: Final = (
        (b"content-type", b"application/json"),
        (b"content-length", b"99"),
        (b"content-encoding", b"gzip"),
        (b"retry-after", b"3"),
        (b"x-ratelimit-reset-requests", b"1m30s"),
        (b"x-litellm-response-cost", b"0.001"),
        (b"x-litellm-response-cost-original", b"0.0002"),
        (b"x-litellm-response-cost-margin-amount", b"0.0008"),
        (b"x-litellm-model-api-base", b"https://private.invalid"),
        (b"x-litellm-model-id", b"deployment-private"),
        (b"x-request-id", b"upstream-private"),
        (b"server", b"private-supplier"),
        (b"anthropic-ratelimit-tokens-limit", b"999"),
        (b"x-ratelimit-remaining-tokens", b"https://private.invalid"),
        (b"www-authenticate", b"Bearer realm=private"),
        (b"access-control-expose-headers", b"x-litellm-model-id, x-litellm-call-id, x-retail-trace-id, content-type"),
    )
    result: Final = dict(customer_response_headers(headers, request_id=_REQUEST_ID))
    assert result[b"content-length"] == b"99" and result[b"content-encoding"] == b"gzip"
    assert result[b"retry-after"] == b"3" and b"x-ratelimit-reset-requests" not in result
    assert b"x-litellm-response-cost" not in result
    assert result[b"x-request-id"] == result[b"x-litellm-call-id"] == _REQUEST_ID.encode()
    assert result[b"access-control-expose-headers"] == b"x-litellm-call-id, x-retail-trace-id, content-type"
    assert all(private not in repr(result) for private in ("private", "original", "margin", "www-authenticate"))


def test_invalid_identifier_and_header_controls_are_never_reflected() -> None:
    result: Final = dict(customer_response_headers(((b"retry-after", b"3\r\nx-private: value"),), request_id="bad\nID"))
    assert result == {}
    error: Final = _mapping(json.loads(customer_error_body(status_code=503, request_id="bad\nID")))
    assert "request_id" not in error


def test_normal_payload_is_untouched_and_only_private_envelope_fields_are_removed() -> None:
    message: Final = {
        "role": "assistant",
        "content": _DETAIL,
        "tool_calls": [
            {
                "function": {"arguments": json.dumps({"provider_specific_fields": _SECRET})},
            }
        ],
    }
    normal: Final = {"model": "public-model", "choices": [{"message": message}], "usage": {"total_tokens": 1}}
    assert customer_response_payload(normal, status_code=200) is None
    assert customer_error_payload(normal, status_code=200) is None
    result: Final = _mapping(
        customer_response_payload(
            {
                **normal,
                "provider_specific_fields": {"upstream": _SECRET},
                "_hidden_params": {"api_base": _DETAIL},
            },
            status_code=200,
        )
    )
    assert result == normal


@pytest.mark.parametrize("split", (1, 7, 29, 63))
def test_sse_error_split_at_arbitrary_bytes_is_sanitized_and_normal_frames_are_exact(split: int) -> None:
    normal: Final = b': ping\r\n\r\ndata: { "choices" : [{"delta":{"content":"literal upstream text"}}] }\n\n'
    error: Final = (
        b"event: error\ndata: "
        + json.dumps(
            {
                "type": "error",
                "error": {"type": "overloaded_error", "message": _DETAIL},
            }
        ).encode()
        + b"\n\n"
    )
    processor: Final = CustomerSSEPrivacy(request_id=_REQUEST_ID)
    wire: Final = normal + error + b"data: [DONE]\n\n"
    result: Final = b"".join(
        processor.feed(wire[index : index + split]) for index in range(0, len(wire), split)
    ) + processor.feed(b"", final=True)
    assert result.startswith(normal) and result.endswith(b"data: [DONE]\n\n")
    assert _SECRET.encode() not in result and b"private.invalid" not in result and _REQUEST_ID.encode() in result


def test_sse_normal_final_tail_and_large_image_frame_preserve_exact_bytes() -> None:
    image: Final = (
        b'event: response.image_generation_call.partial_image\ndata: {"type":"response.image_generation_call.partial_image","partial_image_b64":"'
        + b"a" * 300_000
        + b'"}\n\n'
    )
    tail: Final = b'data: {"choices":[{"delta":{"content":"tail"}}]}'
    processor: Final = CustomerSSEPrivacy()
    assert processor.feed(image) == image
    assert processor.feed(tail) == b""
    assert processor.feed(b"", final=True) == tail


def test_sse_overflow_and_malformed_error_events_fail_closed_then_resume_next_frame() -> None:
    processor: Final = CustomerSSEPrivacy(request_id=_REQUEST_ID, max_frame_bytes=100)
    first: Final = processor.feed(b'event: error\ndata: {"error":{"message":"' + _SECRET.encode() * 30)
    second: Final = processor.feed(b'"}}\n\ndata: {"choices":[]}\n\n')
    assert _SECRET.encode() not in first + second
    assert second == b'data: {"choices":[]}\n\n'
    malformed: Final = processor.feed(b"event: error\ndata: " + _DETAIL.encode() + b"\n\n")
    assert _SECRET.encode() not in malformed and _REQUEST_ID.encode() in malformed


def test_unknown_or_huge_error_codes_do_not_throw() -> None:
    error: Final = customer_error_payload({"error": {"code": "9" * 5000, "message": _DETAIL}}, status_code=200)
    assert error is not None and _SECRET not in json.dumps(error)


@pytest.mark.parametrize(
    "payload",
    (
        {"message": _DETAIL, "code": 503},
        {"type": "error", "message": _DETAIL},
        {"type": "response.failed", "response": {"status": "failed", "error": None}},
    ),
)
def test_error_event_marker_is_authoritative_even_without_the_common_error_wrapper(payload: object) -> None:
    processor: Final = CustomerSSEPrivacy(request_id=_REQUEST_ID)
    wire: Final = b"event: error\ndata: " + json.dumps(payload).encode() + b"\n\n"
    result: Final = processor.feed(wire, final=True)
    assert _SECRET.encode() not in result and _REQUEST_ID.encode() in result and b'"error":' in result


@pytest.mark.parametrize(
    "message",
    (
        "Invalid expression: cache_write() requires an allowlisted usage fact",
        "Missing pricing policy for catalog model private-model",
        "Database relation internal_wallet_snapshot does not exist",
        "SQL parameter internal_price_version is invalid",
        "Driver setting missing: internal_connection_string",
        "Cache write prices are missing for TTL seconds: 3600",
    ),
)
def test_server_configuration_diagnostics_are_private_while_public_parameter_and_code_remain(message: str) -> None:
    result: Final = _mapping(
        customer_error_payload(
            {
                "error": {"message": message, "param": "max_tokens", "code": "invalid_parameter"},
            },
            status_code=422,
            request_id=_REQUEST_ID,
        )
    )
    error: Final = _mapping(result["error"])
    assert error["message"] != message and "Contact support" in str(error["message"])
    assert error["param"] == "max_tokens" and error["code"] == "invalid_parameter"
    assert error["request_id"] == _REQUEST_ID


def test_private_parameter_names_do_not_bypass_safe_message_filter() -> None:
    result: Final = _mapping(
        customer_error_payload(
            {
                "error": {"message": "Invalid request", "param": "internal_wallet_snapshot"},
            },
            status_code=422,
        )
    )
    assert _mapping(result["error"])["param"] is None


@pytest.mark.parametrize(
    ("field", "value"), (("code", 503), ("code", "503"), ("status_code", 503), ("status", "UNAVAILABLE"))
)
def test_bare_sse_error_without_event_marker_is_identified_by_protocol_fields(field: str, value: object) -> None:
    processor: Final = CustomerSSEPrivacy(request_id=_REQUEST_ID)
    wire: Final = b"data: " + json.dumps({"message": _DETAIL, field: value}).encode() + b"\n\n"
    result: Final = processor.feed(wire, final=True)
    assert _SECRET.encode() not in result and b"private.invalid" not in result
    payload: Final = _mapping(json.loads(result.removeprefix(b"data: ")))
    assert _mapping(payload["error"])["request_id"] == _REQUEST_ID


def test_non_json_sse_diagnostic_fails_closed_and_comments_heartbeats_and_done_are_exact() -> None:
    processor: Final = CustomerSSEPrivacy(request_id=_REQUEST_ID)
    prefix: Final = b": keepalive\n\ndata: ping\n\ndata: heartbeat\n\ndata: keep-alive\n\n"
    diagnostic: Final = b"data: " + _DETAIL.encode() + b"\n\n"
    suffix: Final = b"data: [DONE]\n\n"
    result: Final = processor.feed(prefix + diagnostic + suffix, final=True)
    assert result.startswith(prefix) and result.endswith(suffix)
    assert _SECRET.encode() not in result and _REQUEST_ID.encode() in result


def test_bare_error_shape_inside_model_content_or_normal_envelope_is_never_inspected() -> None:
    content: Final = json.dumps({"message": _DETAIL, "code": 503})
    normal: Final = {"message": content, "code": 503, "choices": [{"delta": {"content": content}}]}
    wire: Final = b"data: " + json.dumps(normal).encode() + b"\n\n"
    processor: Final = CustomerSSEPrivacy()
    assert processor.feed(wire, final=True) == wire


def test_bare_sse_invalid_parameter_keeps_safe_user_action_and_field() -> None:
    processor: Final = CustomerSSEPrivacy()
    result: Final = processor.feed(
        b'data: {"message":"max_tokens must be positive","code":"invalid_parameter","param":"max_tokens"}\n\n',
        final=True,
    )
    payload: Final = _mapping(json.loads(result.removeprefix(b"data: ")))
    error: Final = _mapping(payload["error"])
    assert error["message"] == "max_tokens must be positive" and error["param"] == "max_tokens"
    assert error["code"] == "invalid_parameter"


@pytest.mark.parametrize(
    "message",
    (
        "Invalid account ACCT-PRIVATE-4711",
        "max_tokens is invalid; diagnostic=ABC123PRIVATE",
        "max_tokens must be positive; diagnostic=ABC123PRIVATE",
        "messages[0].ABC123PRIVATE is required",
    ),
)
def test_unstructured_actionable_words_cannot_reflect_unknown_secrets(message: str) -> None:
    result: Final = _mapping(
        customer_error_payload(
            {
                "error": {"message": message, "param": "max_tokens", "code": "invalid_parameter"},
            },
            status_code=400,
        )
    )
    error: Final = _mapping(result["error"])
    assert "ACCT-PRIVATE-4711" not in str(error["message"]) and "ABC123PRIVATE" not in str(error["message"])
    assert error["param"] == "max_tokens" and error["code"] == "invalid_parameter"


@pytest.mark.parametrize(
    "name",
    (
        "x-litellm-response-cost",
        "x-litellm-response-cost-input",
        "x-litellm-response-cost-output",
        "x-litellm-response-cost-cache-read",
        "x-litellm-response-cost-cache-creation",
        "x-litellm-response-cost-reasoning",
        "x-litellm-response-cost-tool-usage",
        "x-litellm-response-cost-discount-amount",
        "x-litellm-response-cost-original",
        "x-litellm-response-cost-margin-amount",
        "x-litellm-response-cost-margin-percent",
        "x-litellm-key-spend",
        "x-ratelimit-limit-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
        "ratelimit-limit",
        "ratelimit-remaining",
        "ratelimit-reset",
    ),
)
def test_native_accounting_and_supplier_quota_are_never_exposed_as_customer_prices_or_limits(name: str) -> None:
    assert customer_response_headers(((name.encode(), b"123.456"),)) == ()


def test_only_site_key_controls_and_retry_delay_remain_public() -> None:
    headers: Final = (
        (b"x-litellm-key-rpm-limit", b"60"),
        (b"x-litellm-key-tpm-limit", b"100000"),
        (b"x-litellm-key-max-budget", b"10"),
        (b"retry-after", b"3"),
    )
    assert customer_response_headers(headers) == headers


@pytest.mark.parametrize(
    "field",
    (
        "cost",
        "spend",
        "provider_cost",
        "original_cost",
        "margin_amount",
        "margin_percent",
        "cost_breakdown",
        "model_info",
        "litellm_params",
        "proxy_server_request",
        "litellm_metadata",
    ),
)
def test_normal_success_private_financial_and_configuration_envelope_fields_are_removed(field: str) -> None:
    normal: Final = {
        "model": "public-model",
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "metadata": {"customer_tag": "public"},
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    result: Final = customer_response_payload({**normal, field: "NATIVE-ACCOUNTING-PRIVATE"}, status_code=200)
    assert result == normal


def test_defined_usage_cost_is_removed_without_changing_token_counts_or_user_metadata_and_content() -> None:
    content: Final = {"cost": "model-generated-value", "litellm_params": "model-generated-value"}
    normal: Final = {
        "type": "response.completed",
        "response": {
            "model": "public-model",
            "metadata": {"cost": "customer-value"},
            "output": [{"content": content, "tool_calls": [{"function": {"arguments": json.dumps(content)}}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        },
    }
    response: Final = _mapping(normal["response"])
    usage: Final = _mapping(response["usage"])
    source: Final = {**normal, "response": {**response, "usage": {**usage, "cost": 123.456}}}
    assert customer_response_payload(source, status_code=200) == normal


class _BoundaryWork:
    def __init__(self) -> None:
        self.scanned_bytes = 0
        self.calls = 0

    def __call__(self, data: bytes) -> Iterator[int]:
        self.scanned_bytes += len(data)
        self.calls += 1
        return _frame_boundaries(data)


@pytest.mark.parametrize("chunk_size", (1, 2, 3, 7, 1024))
def test_fragmented_frame_scanning_is_linear_in_new_input_and_preserves_crlf_boundaries(chunk_size: int) -> None:
    work: Final = _BoundaryWork()
    processor: Final = CustomerSSEPrivacy(_scan=work)
    frame: Final = b'data: {"choices":[{"delta":{"content":"' + b"x" * 8192 + b'"}}]}\r\n\r\n'
    wire: Final = b": ping\r\n\r\n" + frame + b'data: {"choices":[]}\n\ndata: [DONE]\r\r'
    result: Final = b"".join(
        processor.feed(wire[index : index + chunk_size]) for index in range(0, len(wire), chunk_size)
    ) + processor.feed(b"", final=True)
    assert result == wire
    assert work.scanned_bytes <= len(wire) + 3 * work.calls


def test_incremental_overflow_drops_only_the_oversized_frame_and_recovers_across_split_crlf() -> None:
    work: Final = _BoundaryWork()
    processor: Final = CustomerSSEPrivacy(max_frame_bytes=100, request_id=_REQUEST_ID, _scan=work)
    prefix: Final = b'data: {"error":{"message":"' + _SECRET.encode() * 30
    first: Final = processor.feed(prefix)
    second: Final = processor.feed(b'"}}\r\n\r')
    third: Final = processor.feed(b'\ndata: {"choices":[]}\r\n\r\n', final=True)
    assert _SECRET.encode() not in first + second + third
    assert _REQUEST_ID.encode() in first and second == b""
    assert third == b'data: {"choices":[]}\r\n\r\n'
    assert work.scanned_bytes <= len(prefix) + 42 + 3 * work.calls


def test_one_byte_fragmentation_retains_frame_bytes_without_a_reference_per_fragment() -> None:
    processor: Final = CustomerSSEPrivacy()
    prefix: Final = b'data: {"choices":[{"delta":{"content":"'
    suffix: Final = b'"}}]}\n\n'
    size: Final = 32 * 1024
    assert processor.feed(prefix) == b""
    was_tracing: Final = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        baseline: Final = tracemalloc.get_traced_memory()[0]
        for _ in range(size):
            assert processor.feed(b"x") == b""
        retained: Final = tracemalloc.get_traced_memory()[0] - baseline
        assert retained < size * 3
    finally:
        if not was_tracing:
            tracemalloc.stop()
    assert processor.feed(suffix, final=True) == prefix + b"x" * size + suffix
