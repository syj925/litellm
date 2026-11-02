from datetime import datetime, timedelta, timezone
from typing import Final

import pytest

from litellm.proxy.retail.trace_store import (
    BODY_RETENTION,
    _BillingRow,
    _body_value,
    _LedgerTraceRow,
    _money,
    _TraceRow,
    redact_trace_value,
    trace_response,
)

_NOW: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_historical_requests_have_no_invented_trace_pricing_or_usage() -> None:
    result: Final = trace_response("historical-call", None, None, (), _NOW)
    assert result["recorded"] is False
    assert result["trace"] is None
    assert result["billing"] is None
    assert result["request_body"] == {"available": False, "expires_at": None, "value": None}
    assert set(result["missing"]) == {"trace", "request_body", "billing", "frozen_pricing", "usage", "provider_cost"}


@pytest.mark.parametrize("elapsed", (BODY_RETENTION, BODY_RETENTION + timedelta(microseconds=1)))
def test_a_stored_expired_body_is_never_returned_even_before_the_cleanup_job_runs(elapsed: timedelta) -> None:
    trace: Final = _TraceRow(
        trace_id="server-id", call_id="call", retail_request_id=None, trace={"phases": []}, request_body={"messages": ["short-lived"]},
        body_expires_at=_NOW + BODY_RETENTION,
    )
    result: Final = trace_response("call", trace, None, (), _NOW + elapsed)
    assert result["recorded"] is True
    assert result["trace"] == {"phases": []}
    assert result["request_body"]["available"] is False
    assert result["request_body"]["value"] is None
    assert "short-lived" not in str(result)


def test_current_bodies_and_frozen_billing_evidence_remain_separate_from_live_model_prices() -> None:
    trace: Final = _TraceRow(
        trace_id="server-id", call_id="call", retail_request_id="retail-request", trace={"phases": []},
        request_body={"messages": [{"content": "diagnostic message"}]}, body_expires_at=_NOW + BODY_RETENTION,
    )
    billing: Final = _BillingRow(
        request_id="retail-request", status="settled", reserved_nanos=1_000_000, settled_nanos=200_000,
        price_snapshot={"price_version": 7, "discount": "0.7", "resource_pool_id": "pool", "resource_source_kind": "api"},
        details={"usage": {"input_tokens": 20}, "provider_cost": "0.0001"},
    )
    entry: Final = _LedgerTraceRow(
        id="ledger-id", kind="charge", amount_nanos=-200_000, created_at=_NOW,
        details={"usage": {"input_tokens": 20}, "provider_cost": "0.0001"},
    )
    result: Final = trace_response("call", trace, billing, (entry,), _NOW)
    assert result["request_body"]["available"] is True
    assert result["billing"]["price_snapshot"] == billing.price_snapshot
    assert result["billing"]["provider_cost_source"] == "gateway_cost_calculation"
    assert result["billing"]["settled_amount"] == "0.000200000"
    assert result["billing"]["resource"]["pool_id"] == "pool"
    assert result["billing"]["ledger"][0]["amount"] == "-0.000200000"


@pytest.mark.parametrize("value", (None, "NaN", "Infinity", "-1", True, {}, []))
def test_missing_invalid_or_negative_provider_cost_never_becomes_a_zero_estimate(value: object) -> None:
    assert _money(value) is None


def test_pending_reservations_do_not_claim_a_customer_charge_or_provider_cost() -> None:
    billing: Final = _BillingRow(
        request_id="pending", status="reserved", reserved_nanos=1_000_000, settled_nanos=None,
        price_snapshot={"price_version": 1}, details={"billing_error": "Usage not reported"},
    )
    result: Final = trace_response("call", None, billing, (), _NOW)
    assert result["billing"]["settled_amount"] is None
    assert result["billing"]["provider_cost"] is None
    assert result["billing"]["usage"] is None


def test_nested_credentials_and_embedded_bearer_tokens_are_removed_from_diagnostics() -> None:
    value: Final = redact_trace_value({
        "headers": {"Authorization": "Bearer private-auth", "cookie": "session=private-cookie"},
        "nested": {"api_key": "private-key", "password": "private-password", "access_token": "private-token",
                   "token": "private-token", "private_key": "private-signing-key"},
        "input_tokens": 10, "friendly_alias": "shared credential",
        "messages": [{"content": "Use Bearer private-token-value to authenticate"}],
    })
    assert all(secret not in str(value) for secret in (
        "private-auth", "private-cookie", "private-key", "private-password", "private-token", "private-signing-key",
    ))
    assert value["input_tokens"] == 10
    assert value["friendly_alias"] == "shared credential"
    assert value["messages"][0]["content"] == "Use Bearer [REDACTED] to authenticate"


def test_oversized_bodies_are_replaced_with_a_storage_limit_notice() -> None:
    result: Final = _body_value({"messages": [{"content": "x" * 16_384} for _ in range(10)]})
    assert result["truncated"] is True
    assert "messages" not in result
