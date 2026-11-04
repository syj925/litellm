from typing import Final

import psycopg
import pytest
from fastapi import HTTPException, Request
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from litellm.proxy.retail.organization_reports import _execute, _organization_id, _queries, require_organization_reports
from litellm.proxy.retail.report_store import ReportError


@pytest.mark.asyncio
async def test_successful_storage_result_is_preserved() -> None:
    receipt: Final = ("frozen-report", "0.123456789")

    async def stored() -> tuple[str, str]:
        return receipt

    assert await _execute(stored()) is receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("error", (
    psycopg.DatabaseError("private-fixture-database-detail"), PoolClosed("private-fixture-pool"),
    PoolTimeout("private-fixture-timeout"), TooManyRequests("private-fixture-waiters"),
))
async def test_database_and_pool_errors_have_fixed_private_message(error: Exception) -> None:
    async def failed() -> bytes:
        raise error

    with pytest.raises(HTTPException) as caught:
        await _execute(failed())
    assert caught.value.status_code == 503
    assert "private-fixture" not in caught.value.detail
    assert caught.value.headers is not None
    assert caught.value.headers["Cache-Control"] == "private, no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", (
    ("report_not_found", 404), ("organization_not_found", 404), ("report_expired", 410),
    ("report_not_ready", 409), ("idempotency_conflict", 409), ("row_limit_exceeded", 422),
    ("snapshot_inconsistent", 503), ("private-fixture-unrecognized-error", 503),
))
async def test_report_errors_keep_status_without_exposing_internal_codes(code: str, status: int) -> None:
    async def failed() -> bytes:
        raise ReportError(code, status)

    with pytest.raises(HTTPException) as caught:
        await _execute(failed())
    assert caught.value.status_code == status
    assert "private-fixture" not in caught.value.detail
    assert caught.value.headers is not None
    assert caught.value.headers["Vary"] == "Authorization, Cookie"


@pytest.mark.parametrize("query", (b"account_id=private-fixture-value", b"kind=charge", b"organization_id=org-b"))
def test_unknown_query_cannot_change_report_scope_or_echo_values(query: bytes) -> None:
    request: Final = Request({"type": "http", "query_string": query})
    with pytest.raises(HTTPException) as caught:
        _queries(request, frozenset(("limit", "offset")))
    assert caught.value.status_code == 422
    assert "private-fixture" not in caught.value.detail


@pytest.mark.parametrize("identifier", ("", " private-fixture-value", "private-fixture-value ",
                                         "private-fixture-value\x00", "x" * 192))
def test_malformed_organization_identifiers_are_fixed_private_validation_errors(identifier: str) -> None:
    with pytest.raises(HTTPException) as caught:
        _organization_id(identifier)
    assert caught.value.status_code == 422
    assert "private-fixture" not in caught.value.detail
    assert caught.value.headers is not None
    assert caught.value.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("disabled", (
    "RETAIL_ORG_FINANCE_ENABLED", "RETAIL_REPORT_EXPORT_ENABLED", "RETAIL_ORGANIZATIONS_ENABLED",
    "RETAIL_OPERATION_AUDIT_ENABLED",
))
def test_both_modules_and_their_dependencies_are_required(
    monkeypatch: pytest.MonkeyPatch, disabled: str,
) -> None:
    for flag in ("RETAIL_ORG_FINANCE_ENABLED", "RETAIL_REPORT_EXPORT_ENABLED", "RETAIL_ORGANIZATIONS_ENABLED",
                 "RETAIL_OPERATION_AUDIT_ENABLED"):
        monkeypatch.setenv(flag, "true")
    monkeypatch.setenv(disabled, "false")
    with pytest.raises(HTTPException) as caught:
        require_organization_reports()
    assert caught.value.status_code == 404
    assert caught.value.headers is not None
    assert caught.value.headers["Cache-Control"] == "private, no-store"
