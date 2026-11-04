import csv
import hashlib
import io
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Final
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import nanos_to_amount
from .organization_finance_store import ConsumptionState
from .report_store import ReportError, ReportLimits, csv_text

_BEIJING: Final = ZoneInfo("Asia/Shanghai")
_HEADERS: Final = ("sequence", "model", "captured_at_utc", "captured_at_beijing", "state", "settled_nanos", "settled_usd")


class DelegatedReportRequestRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    sequence: int = Field(ge=1)
    model: str
    captured_at: datetime
    state: ConsumptionState
    settled_nanos: int | None

    @model_validator(mode="after")
    def settled_amount(self) -> "DelegatedReportRequestRow":
        if (self.state == "settled") != (self.settled_nanos is not None):
            raise ValueError("Only settled requests have a fixed charge")
        if self.settled_nanos is not None and self.settled_nanos < 0:
            raise ValueError("A settled charge cannot be negative")
        return self


def delegated_report_csv(rows: Sequence[DelegatedReportRequestRow], limits: ReportLimits) -> bytes:
    if len(rows) > min(10000, limits.max_rows):
        raise ReportError("row_limit_exceeded", 422)
    output: Final = io.StringIO(newline="")
    writer: Final = csv.writer(output, lineterminator="\r\n")
    writer.writerow(_HEADERS)
    for row in rows:
        writer.writerow((str(row.sequence), csv_text(row.model), row.captured_at.astimezone(timezone.utc).isoformat(),
                         row.captured_at.astimezone(_BEIJING).isoformat(), row.state,
                         str(row.settled_nanos) if row.settled_nanos is not None else "",
                         nanos_to_amount(row.settled_nanos) if row.settled_nanos is not None else ""))
        if output.tell() > limits.max_bytes:
            raise ReportError("artifact_too_large", 422)
    content: Final = output.getvalue().encode("utf-8")
    if len(content) > limits.max_bytes:
        raise ReportError("artifact_too_large", 422)
    return content


def render_delegated_artifact(rows: Sequence[object], limits: ReportLimits) -> tuple[bytes, str]:
    validated: Final = tuple(DelegatedReportRequestRow.model_validate(row) for row in rows)
    content: Final = delegated_report_csv(validated, limits)
    return content, hashlib.sha256(content).hexdigest()
