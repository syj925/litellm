import csv
import io
from datetime import datetime, timezone
from decimal import Decimal
from typing import Final

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.delegated_report_csv import DelegatedReportRequestRow, delegated_report_csv
from litellm.proxy.retail.report_store import ReportError, ReportLimits

_AT: Final = datetime(2020, 1, 2, tzinfo=timezone.utc)


def row(*, model: str = "model", nanos: int | None = 7, state: str = "settled") -> DelegatedReportRequestRow:
    return DelegatedReportRequestRow.model_validate({
        "sequence": 1, "model": model, "captured_at": _AT, "state": state, "settled_nanos": nanos,
        "user_id": "private-customer@example.test", "request_id": "private-request", "assignment_version": 99,
        "organization_version": 98, "membership_version": 97, "provider_cost": "private-purchase", "api_key": "credential",
    })


def test_delegated_csv_materializes_only_safe_projection_and_exact_money() -> None:
    original: Final = row(nanos=2**53+7)
    content: Final = delegated_report_csv((original,), ReportLimits())
    exported: Final = next(csv.DictReader(io.StringIO(content.decode())))
    assert set(exported) == {"sequence", "model", "captured_at_utc", "captured_at_beijing", "state", "settled_nanos", "settled_usd"}
    assert int(exported["settled_nanos"]) == original.settled_nanos
    assert Decimal(exported["settled_usd"])*1_000_000_000 == original.settled_nanos
    assert all(text not in content.decode() for text in ("private", "credential", "version", "user_id", "request_id", "cost"))


@pytest.mark.parametrize("state", ("unknown", "conflict", "pending", "released"))
def test_unsettled_delegated_rows_have_blank_amounts(state: str) -> None:
    exported: Final = next(csv.DictReader(io.StringIO(delegated_report_csv((row(nanos=None, state=state),), ReportLimits()).decode())))
    assert exported["settled_nanos"] == exported["settled_usd"] == ""
    assert exported["state"] == state


@pytest.mark.parametrize("text", ("=SUM(1,2)", " +1", "-1", "@cmd", "\tformula", "\rformula", "\nformula"))
def test_delegated_model_names_cannot_execute_as_spreadsheet_formulas(text: str) -> None:
    exported: Final = next(csv.DictReader(io.StringIO(delegated_report_csv((row(model=text),), ReportLimits()).decode())))
    assert exported["model"] == "'"+text


@pytest.mark.parametrize(("state", "nanos"), (("settled", None), ("settled", -1), ("unknown", 0), ("pending", 7)))
def test_delegated_rows_reject_inconsistent_state_and_money(state: str, nanos: int | None) -> None:
    with pytest.raises(ValidationError):
        row(nanos=nanos, state=state)


def test_delegated_export_budgets_cannot_be_raised_by_account_export_settings() -> None:
    with pytest.raises(ReportError, match="row_limit_exceeded"):
        delegated_report_csv((row(),)*10001, ReportLimits(max_rows=100000))
    with pytest.raises(ReportError, match="artifact_too_large"):
        delegated_report_csv((row(model="测"*1000),), ReportLimits(max_bytes=1024))
