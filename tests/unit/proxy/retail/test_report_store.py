import csv
import io
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import UUID

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.report_store import (
    ReportError,
    ReportLedgerRow,
    ReportLimits,
    ReportWindow,
    _JobRow,
    _TotalsRow,
    byte_range,
    csv_text,
    open_day_window,
    report_csv,
    report_digest,
    totals_view,
    validate_window,
)

_AT: Final = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("end", "partial"),
    (
        ("2026-01-01T16:00:00+00:00", False),
        ("2026-01-02T00:00:00+08:00", False),
        ("2026-01-02T00:00:00+00:00", True),
        ("2026-01-02T12:00:00+00:00", True),
        ("2026-01-03T00:00:00+08:00", True),
    ),
)
def test_report_including_open_beijing_day_is_partial_even_before_snapshot_time(end: str, partial: bool) -> None:
    window: Final = ReportWindow(start_at=datetime(2026, 1, 1, tzinfo=timezone.utc), end_at=datetime.fromisoformat(end))
    assert open_day_window(window, datetime(2026, 1, 2, 12, tzinfo=timezone.utc)) is partial


def _row(
    *,
    model: str = "published-model",
    details: Mapping[str, str] | None = None,
    payload: Mapping[str, str] | None = None,
) -> ReportLedgerRow:
    return ReportLedgerRow.model_validate(
        {
            "ledger_id": "00000000-0000-0000-0000-000000000001",
            "kind": "charge",
            "amount_nanos": -9007199254740993,
            "balance_after_nanos": -123456789,
            "request_id": "request",
            "model": model,
            "created_at": _AT,
            "details": details,
            "payload": payload,
        }
    )


def test_report_money_and_csv_preserve_signed_nanos_above_javascript_precision() -> None:
    row: Final = _row()
    exported: Final = tuple(csv.DictReader(io.StringIO(report_csv((row,), ReportLimits()).decode())))
    assert exported[0]["amount_nanos"] == "-9007199254740993"
    assert exported[0]["amount_usd"] == "-9007199.254740993"
    assert exported[0]["balance_after_nanos"] == "-123456789"
    assert exported[0]["balance_after_usd"] == "-0.123456789"
    assert exported[0]["created_at_utc"] == "2026-01-02T00:00:00+00:00"
    assert exported[0]["created_at_beijing"] == "2026-01-02T08:00:00+08:00"
    totals: Final = totals_view(_TotalsRow(credit=9007199254740999, charge=9007199254740993, release=0))
    assert totals.credit_nanos == "9007199254740999"
    assert totals.charge_nanos == "9007199254740993"
    assert totals.charge == "9007199.254740993"


@pytest.mark.parametrize("text", ("=SUM(1,2)", "+cmd", "-cmd", "@SUM(1)", "  =cmd", "\tcmd", "\rcmd", "\ncmd"))
def test_export_quotes_untrusted_spreadsheet_formulas_without_altering_money(text: str) -> None:
    exported: Final = tuple(csv.DictReader(io.StringIO(report_csv((_row(model=text),), ReportLimits()).decode())))
    assert exported[0]["model"] == "'" + text
    assert exported[0]["amount_nanos"].startswith("-")
    assert not exported[0]["amount_nanos"].startswith("'")


def test_csv_projection_cannot_copy_provider_costs_request_bodies_or_credentials() -> None:
    row: Final = _row(
        details={"provider_cost": "private-cost", "api_key": "private-key"}, payload={"body": "private-body"}
    )
    exported: Final = report_csv((row,), ReportLimits()).decode()
    assert "private-cost" not in exported
    assert "private-key" not in exported
    assert "private-body" not in exported
    assert "request" in exported
    assert csv_text(None) == ""
    assert csv_text("published-model") == "published-model"


def test_csv_bounds_utf8_bytes_and_row_count_instead_of_silently_truncating() -> None:
    with pytest.raises(ReportError, match="artifact_too_large"):
        report_csv((_row(model="界" * 800),), ReportLimits(max_bytes=1024))
    with pytest.raises(ReportError, match="row_limit_exceeded"):
        report_csv((_row(), _row()), ReportLimits(max_rows=1))


@pytest.mark.parametrize(
    ("header", "length", "expected"),
    (
        (None, 10, None),
        ("bytes=0-3", 10, (0, 3)),
        ("bytes=4-", 10, (4, 9)),
        ("bytes=-3", 10, (7, 9)),
        ("bytes=3-999", 10, (3, 9)),
        ("bytes=-999", 10, (0, 9)),
    ),
)
def test_single_range_download_uses_inclusive_http_byte_offsets(
    header: str | None,
    length: int,
    expected: tuple[int, int] | None,
) -> None:
    assert byte_range(header, length) == expected


@pytest.mark.parametrize(
    "header",
    ("bytes=10-", "bytes=3-2", "bytes=-0", "bytes=-", "bytes=0-1,3-4", "items=0-1", "bytes=" + "1" * 200 + "-"),
)
def test_invalid_ranges_fail_closed_without_interpreting_multiple_ranges(header: str) -> None:
    with pytest.raises(ReportError) as error:
        byte_range(header, 10)
    assert error.value.status == 416


def test_report_filters_canonicalize_offset_equivalent_windows_and_reject_unknown_scope() -> None:
    utc: Final = ReportWindow(start_at=_AT, end_at=_AT + timedelta(days=1))
    offset: Final = ReportWindow(start_at="2026-01-02T08:00:00+08:00", end_at="2026-01-03T08:00:00+08:00")
    assert report_digest("account", utc) == report_digest("account", offset)
    assert report_digest("other", utc) != report_digest("account", utc)
    assert report_digest("account", utc.model_copy(update={"kind": "charge"})) != report_digest("account", utc)
    with pytest.raises(ValidationError):
        ReportWindow.model_validate({**utc.model_dump(), "organization_id": "untrusted-scope"})
    with pytest.raises(ValidationError):
        ReportWindow(start_at="2026-01-02T00:00:00", end_at=_AT + timedelta(days=1))
    with pytest.raises(ValidationError):
        ReportWindow(start_at=_AT, end_at=_AT)


def test_window_limits_reject_future_or_oversized_exports() -> None:
    with pytest.raises(ReportError, match="window_too_large"):
        validate_window(ReportWindow(start_at=_AT, end_at=_AT + timedelta(days=32)), _AT, ReportLimits())
    with pytest.raises(ReportError, match="future_window"):
        validate_window(
            ReportWindow(start_at=_AT + timedelta(days=1), end_at=_AT + timedelta(days=2)), _AT, ReportLimits()
        )
    validate_window(ReportWindow(start_at=_AT, end_at=_AT + timedelta(days=1)), _AT, ReportLimits())


def test_public_job_does_not_serialize_creator_digests_or_artifact_storage() -> None:
    job: Final = _JobRow.model_validate(
        {
            "id": UUID(int=1),
            "creator_id": "actor",
            "account_id": "account",
            "request_digest": "private-digest",
            "state": "ready",
            "start_at": _AT,
            "end_at": _AT + timedelta(days=1),
            "snapshot_at": _AT,
            "created_at": _AT,
            "expires_at": _AT + timedelta(days=1),
            "partial": True,
            "row_count": 1,
            "summary": totals_view(_TotalsRow(credit=0, charge=5, release=0)).model_dump(),
            "unresolved": {"unknown": 2},
            "artifact_bytes": 10,
            "artifact_sha256": "private-artifact-digest",
            "artifact": b"private-body",
            "error_code": None,
        }
    )
    public: Final = job.public().model_dump(mode="json")
    assert public["scope"] == "account"
    assert public["total_rows"] == 1
    assert public["unresolved"] == {"pending": 0, "unknown": 2, "conflict": 0}
    assert "private" not in str(public)
    assert "creator_id" not in public
