import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Final

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.organization_report_csv import OrganizationReportRequestRow, organization_report_csv
from litellm.proxy.retail.organization_report_store import OrganizationReportWindow, organization_report_digest
from litellm.proxy.retail.report_store import ReportError, ReportLimits

_AT: Final = datetime(2020, 1, 2, tzinfo=timezone.utc)


def row(*, text: str = "model", nanos: int | None = 7, state: str = "settled") -> OrganizationReportRequestRow:
    return OrganizationReportRequestRow.model_validate({
        "request_id": text, "user_id": text, "model": text, "captured_at": _AT, "state": state,
        "settled_nanos": nanos, "assignment_version": 1, "organization_version": 2, "membership_version": 3,
    })


@pytest.mark.parametrize("state", ("pending", "released", "unknown", "conflict"))
def test_unsettled_request_csv_keeps_unknown_charge_blank(state: str) -> None:
    content: Final = organization_report_csv((row(nanos=None, state=state),), ReportLimits())
    exported: Final = next(csv.DictReader(io.StringIO(content.decode())))
    assert exported["state"] == state
    assert exported["settled_nanos"] == exported["settled_usd"] == ""


@pytest.mark.parametrize(("state", "nanos"), (("settled", None), ("settled", -1), ("unknown", 0), ("pending", 7)))
def test_csv_row_refuses_inconsistent_state_or_amount(state: str, nanos: int | None) -> None:
    with pytest.raises(ValidationError):
        row(nanos=nanos, state=state)


@pytest.mark.parametrize("text", ("=SUM(1,2)", " +1", "-1", "@cmd", "\tformula", "\rformula", "\nformula"))
def test_csv_protects_all_request_identifier_columns_from_spreadsheet_formulas(text: str) -> None:
    exported: Final = next(csv.DictReader(io.StringIO(organization_report_csv((row(text=text),), ReportLimits()).decode())))
    assert exported["request_id"] == exported["user_id"] == exported["model"] == "'"+text


def test_csv_money_preserves_nanos_above_javascript_integer_precision_and_projects_only_safe_fields() -> None:
    original: Final = row(nanos=2**53+7)
    projected: Final = OrganizationReportRequestRow.model_validate({
        **original.model_dump(), "provider_cost": "secret", "api_key": "credential", "request_body": "private",
    })
    content: Final = organization_report_csv((projected,), ReportLimits())
    exported: Final = next(csv.DictReader(io.StringIO(content.decode())))
    assert int(exported["settled_nanos"]) == original.settled_nanos
    assert Decimal(exported["settled_usd"])*1_000_000_000 == original.settled_nanos
    assert set(exported) == {
        "request_id", "user_id", "model", "captured_at_utc", "captured_at_beijing", "state",
        "settled_nanos", "settled_usd", "assignment_version", "organization_version", "membership_version",
    }
    assert "secret" not in content.decode() and "credential" not in content.decode() and "private" not in content.decode()


def test_csv_limits_rows_and_utf8_bytes_without_truncating() -> None:
    with pytest.raises(ReportError, match="row_limit_exceeded"):
        organization_report_csv((row(), row()), ReportLimits(max_rows=1))
    with pytest.raises(ReportError, match="artifact_too_large"):
        organization_report_csv((row(text="测"*1000),), ReportLimits(max_bytes=1024))
    with pytest.raises(ReportError, match="row_limit_exceeded"):
        organization_report_csv((row(),)*10001, ReportLimits(max_rows=100000))


def test_organization_digest_canonicalizes_equal_instants_and_separates_scope() -> None:
    window: Final = OrganizationReportWindow(start_at=_AT, end_at=_AT+timedelta(days=1))
    offset: Final = timezone(timedelta(hours=8))
    equivalent: Final = OrganizationReportWindow(
        start_at=_AT.astimezone(offset), end_at=(_AT+timedelta(days=1)).astimezone(offset),
    )
    assert organization_report_digest("org-a", window) == organization_report_digest("org-a", equivalent)
    assert organization_report_digest("org-a", window) != organization_report_digest("org-b", equivalent)


@pytest.mark.parametrize("extra", ("kind", "account_id", "axis", "organization_id", "scope", "include_provider_cost"))
def test_organization_window_has_no_account_ledger_or_arbitrary_projection_filters(extra: str) -> None:
    with pytest.raises(ValidationError):
        OrganizationReportWindow.model_validate({"start_at": _AT, "end_at": _AT+timedelta(days=1), extra: "value"})


def test_organization_window_refuses_naive_or_reversed_times() -> None:
    with pytest.raises(ValidationError):
        OrganizationReportWindow(start_at=_AT.replace(tzinfo=None), end_at=_AT)
    with pytest.raises(ValidationError):
        OrganizationReportWindow(start_at=_AT, end_at=_AT-timedelta(seconds=1))
