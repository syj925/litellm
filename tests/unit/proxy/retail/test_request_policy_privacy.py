import json
import logging
import traceback
from collections.abc import Iterator
from contextlib import nullcontext
from typing import Final

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

import litellm
from litellm import _logging, utils
from litellm.litellm_core_utils.redact_messages import should_redact_message_logging
from litellm.proxy.retail.customer_response_middleware import CustomerResponsePrivacyMiddleware
from litellm.proxy.retail.customer_response_privacy import (
    CustomerSSEPrivacy,
    customer_error_payload,
    customer_response_payload,
)
from litellm.proxy.retail.request_policy_privacy import (
    REQUEST_POLICY_PROMPT_MARKER,
    RequestPolicyIngressMiddleware,
    mark_private_policy_prompt,
    policy_prompt_private,
    private_policy_authority_io,
    private_policy_log_value,
)

_PRIVATE: Final = "Private managed instruction for this synthetic request"


@pytest.fixture(autouse=True)
def isolated_privacy_context() -> Iterator[None]:
    mark_private_policy_prompt(False)
    yield
    mark_private_policy_prompt(False)


@pytest.mark.parametrize("path", ("metadata", "litellm_metadata", "user_api_key_metadata", "litellm_params"))
def test_nested_server_prompt_marker_survives_logging_handoffs(path: str) -> None:
    assert policy_prompt_private({path: {REQUEST_POLICY_PROMPT_MARKER: True}})
    assert not policy_prompt_private({path: {REQUEST_POLICY_PROMPT_MARKER: False}})


def test_explicit_logging_header_cannot_disable_server_prompt_redaction() -> None:
    details: Final = {"litellm_params": {"metadata": {
        "headers": {"litellm-disable-message-redaction": "true"},
        "user_api_key_metadata": {REQUEST_POLICY_PROMPT_MARKER: True},
    }}}
    assert should_redact_message_logging(details) is True
    mark_private_policy_prompt(True)
    assert should_redact_message_logging({"litellm_params": {"turn_off_message_logging": False}}) is True


def test_diagnostic_projection_omits_prompt_fields_but_keeps_accounting_values() -> None:
    source: Final = {"usage": {"prompt_tokens": 11, "completion_tokens": 3}, "cost": "0.25",
        "nested": {"instructions": _PRIVATE, "error_message": _PRIVATE, "traceback": _PRIVATE}}
    redacted: Final = private_policy_log_value(source)
    assert redacted == {"usage": {"prompt_tokens": 11, "completion_tokens": 3}, "cost": "0.25",
        "nested": {"instructions": "[Managed prompt details omitted]",
                   "error_message": "[Managed prompt details omitted]", "traceback": "[Managed prompt details omitted]"}}
    assert source["nested"]["instructions"] == _PRIVATE


def test_private_responses_errors_drop_echoed_request_output_and_parameter() -> None:
    mark_private_policy_prompt(True)
    result: Final = customer_error_payload({"type": "response.failed", "response": {
        "id": _PRIVATE, "status": "failed", "output": [{"text": _PRIVATE}],
        "error": {"message": _PRIVATE, "param": _PRIVATE, "type": "invalid_request_error"},
    }}, status_code=400)
    assert isinstance(result, dict)
    assert result["response"]["id"] == "resp_policy_error" and result["response"]["status"] == "failed"
    assert "output" not in result["response"] and _PRIVATE not in json.dumps(result)
    assert result["response"]["error"]["param"] is None


def test_private_sse_error_frames_are_generic_without_changing_successful_output() -> None:
    mark_private_policy_prompt(True)
    cleaner: Final = CustomerSSEPrivacy(status_code=200, request_id="safe-request")
    success: Final = b'data: {"choices":[{"delta":{"content":"visible output"}}]}\n\n'
    failure: Final = b'data: ' + json.dumps({"error": {"message": _PRIVATE, "type": "invalid_request_error"}}).encode() + b'\n\n'
    first: Final = cleaner.feed(success + failure[:17])
    second: Final = cleaner.feed(failure[17:] + b'data: [DONE]\n\n', final=True)
    assert b"visible output" in first + second and b"[DONE]" in first + second
    assert _PRIVATE.encode() not in first + second and b'"error"' in first + second


@pytest.mark.parametrize("nested", (False, True))
def test_successful_responses_hide_managed_request_fields_and_keep_output(nested: bool) -> None:
    response: Final = {"id": "resp_public", "object": "response", "instructions": _PRIVATE,
        "input": [{"role": "system", "content": _PRIVATE}], "output": [{"text": "Public model output"}],
        "usage": {"input_tokens": 11, "output_tokens": 3}}
    source: Final = {"type": "response.completed", "response": response} if nested else response
    assert customer_response_payload(source, status_code=200) is None
    mark_private_policy_prompt(True)
    projected: Final = customer_response_payload(source, status_code=200)
    assert isinstance(projected, dict) and _PRIVATE not in json.dumps(projected)
    body: Final = projected["response"] if nested else projected
    assert body["id"] == "resp_public" and body["output"] == [{"text": "Public model output"}]
    assert body["usage"] == {"input_tokens": 11, "output_tokens": 3}


def test_successful_responses_sse_events_cannot_reflect_managed_instructions() -> None:
    mark_private_policy_prompt(True)
    cleaner: Final = CustomerSSEPrivacy(status_code=200, request_id="safe-request")
    event: Final = b'event: response.completed\ndata: ' + json.dumps({"type": "response.completed", "response": {
        "id": "resp_public", "instructions": _PRIVATE, "output": [{"text": "Public model output"}],
    }}).encode() + b'\n\n'
    result: Final = cleaner.feed(event[:33]) + cleaner.feed(event[33:], final=True)
    assert _PRIVATE.encode() not in result and b"Public model output" in result and b"response.completed" in result


class _Application:
    def __init__(self, *, error: bool = False, scope_marker: bool = False, log: bool = False) -> None:
        self.error = error
        self.scope_marker = scope_marker
        self.log = log
        self.calls = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        if self.scope_marker:
            scope[REQUEST_POLICY_PROMPT_MARKER] = True  # rebind-ok: emulate the trusted server marker across ASGI tasks
        if self.log:
            logging.getLogger("request_policy_fixture").warning("Private data %s", _PRIVATE)
        if self.error:
            raise RuntimeError(_PRIVATE)
        response: Final = JSONResponse({"error": {"message": _PRIVATE, "type": "invalid_request_error"}}, status_code=400)
        await response(scope, receive, send)


class _WebSocketFixture:
    def __init__(self) -> None:
        self.messages: tuple[Message, ...] = ()
        self.calls = 0

    async def send(self, message: Message) -> None:
        self.messages = (*self.messages, message)

    async def receive(self) -> Message:
        return {"type": "websocket.connect"}

    async def app(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 1000})


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", (True, False))
async def test_policy_http_only_contract_closes_websocket_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    for name in ("RETAIL_OPERATION_AUDIT_ENABLED", "RETAIL_NATIVE_AUDIT_ENABLED",
                 "RETAIL_REQUIRE_COORDINATION_REDIS", "RETAIL_BILLING_ENABLED"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", str(enabled).lower())
    fixture: Final = _WebSocketFixture()
    app: Final = RequestPolicyIngressMiddleware(fixture.app)
    await app({"type": "websocket", "path": "/v1/realtime"}, fixture.receive, fixture.send)
    assert fixture.calls == (0 if enabled else 1)
    assert fixture.messages == (({"type": "websocket.close", "code": 1008},) if enabled else (
        {"type": "websocket.accept"}, {"type": "websocket.close", "code": 1000},
    ))


@pytest.mark.asyncio
async def test_ingress_blocks_unauthenticated_passthrough_before_the_endpoint_runs() -> None:
    upstream: Final = _Application()
    app: Final = RequestPolicyIngressMiddleware(upstream, passthrough_denied=lambda scope: True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response: Final = await client.post("/raw/provider", json={"input": "client text"})
    assert response.status_code == 503 and upstream.calls == 0
    assert "unavailable" in response.json()["error"]["message"] and _PRIVATE not in response.text


@pytest.mark.asyncio
async def test_shared_scope_restores_private_error_projection_across_task_contexts() -> None:
    app: Final = RequestPolicyIngressMiddleware(
        CustomerResponsePrivacyMiddleware(_Application(scope_marker=True)), passthrough_denied=lambda scope: False,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response: Final = await client.post("/v1/chat/completions", json={"messages": []})
    assert response.status_code == 400 and _PRIVATE not in response.text
    assert response.json()["error"]["param"] is None and not policy_prompt_private()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ("", "/gateway"))
async def test_management_logging_is_private_and_context_is_released(caplog: pytest.LogCaptureFixture, prefix: str) -> None:
    app: Final = RequestPolicyIngressMiddleware(_Application(log=True), passthrough_denied=lambda scope: False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, root_path=prefix), base_url="http://fixture") as client:
        await client.post(prefix + "/retail/admin/request-policies/prompts", json={"content": "operator text"})
    assert _PRIVATE not in caplog.text
    logging.getLogger("request_policy_fixture").warning("Ordinary diagnostic remains visible")
    assert "Ordinary diagnostic remains visible" in caplog.text


def test_policy_authority_lookup_logs_are_private_before_prompt_resolution(caplog: pytest.LogCaptureFixture) -> None:
    RequestPolicyIngressMiddleware(_Application(), passthrough_denied=lambda scope: False)
    with private_policy_authority_io():
        logging.getLogger("policy_lookup_fixture").warning("Resolved prompt row %s", _PRIVATE)
    assert _PRIVATE not in caplog.text and not policy_prompt_private()
    logging.getLogger("policy_lookup_fixture").warning("Authority diagnostics outside private lookup")
    assert "Authority diagnostics outside private lookup" in caplog.text


@pytest.mark.parametrize("private_scope", ("prompt", "lookup"))
def test_verbose_stdout_omits_private_prompt_and_authority_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], private_scope: str,
) -> None:
    monkeypatch.setattr(litellm, "set_verbose", True)
    monkeypatch.setattr(_logging, "set_verbose", True)
    if private_scope == "prompt":
        mark_private_policy_prompt(True)
    with nullcontext() if private_scope == "prompt" else private_policy_authority_io():
        utils.print_verbose(_PRIVATE)
        _logging.print_verbose(_PRIVATE)
    assert _PRIVATE not in capsys.readouterr().out
    mark_private_policy_prompt(False)
    utils.print_verbose("Ordinary SDK diagnostic")
    _logging.print_verbose("Ordinary SDK diagnostic")
    assert capsys.readouterr().out.count("Ordinary SDK diagnostic") == 2


@pytest.mark.asyncio
async def test_unhandled_private_exceptions_cannot_reappear_in_outer_server_tracebacks() -> None:
    app: Final = RequestPolicyIngressMiddleware(_Application(error=True, scope_marker=True), passthrough_denied=lambda scope: False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        with pytest.raises(RuntimeError) as error:
            await client.post("/v1/chat/completions", json={"messages": []})
    rendered: Final = "".join(traceback.format_exception(error.value))
    assert _PRIVATE not in rendered and "Request policy protected request failed" in rendered
    assert not policy_prompt_private()
