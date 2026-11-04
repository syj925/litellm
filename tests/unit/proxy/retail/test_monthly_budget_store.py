from datetime import datetime, timedelta, timezone
from typing import Final

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.monthly_budget_store import (
    CreateMonthlyBudget,
    StopMonthlyBudget,
    following_month,
    month_floor,
    parse_month,
)


@pytest.mark.parametrize("zone", ("UTC", "Asia/Shanghai"))
@pytest.mark.parametrize("month", ("2024-02", "2026-12", "2027-01"))
def test_calendar_months_preserve_local_midnight_and_real_month_lengths(zone: str, month: str) -> None:
    data: Final = CreateMonthlyBudget(model="public-model", start_month=month, timezone=zone, limit_usd="1")
    start: Final = parse_month(data.start_month, data.timezone)
    end: Final = following_month(start, data.timezone)
    assert month_floor(start + timedelta(seconds=1), data.timezone) == start
    assert month_floor(end - timedelta(microseconds=1), data.timezone) == start
    assert following_month(end, data.timezone) > end
    assert end - start in (timedelta(days=29), timedelta(days=31))
    assert data.limit_usd == "1.000000000"


def test_shanghai_month_boundary_is_distinct_from_utc_and_naive_clocks_are_rejected() -> None:
    at: Final = datetime(2026, 2, 28, 17, tzinfo=timezone.utc)
    assert month_floor(at, "Asia/Shanghai") == datetime(2026, 2, 28, 16, tzinfo=timezone.utc)
    assert month_floor(at, "UTC") == datetime(2026, 2, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="timezone-aware"):
        month_floor(at.replace(tzinfo=None), "UTC")


@pytest.mark.parametrize("field,value", (("timezone", "America/New_York"), ("currency", "CNY"), ("model", "public-*"),
                                      ("start_month", "2026-1"), ("start_month", "0000-01"), ("start_month", "2026-13"),
                                      ("limit_usd", "0"), ("limit_usd", "1.0000000001"), ("user_id", "other")))
def test_plan_input_rejects_ambiguous_scope_currency_months_and_amounts(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        CreateMonthlyBudget.model_validate({"model": "public-model", "start_month": "2026-12", "limit_usd": "1", field: value})


@pytest.mark.parametrize("version", (True, "1", 1.5, 0))
def test_plan_cas_versions_are_strict_positive_integers(version: object) -> None:
    with pytest.raises(ValidationError):
        StopMonthlyBudget.model_validate({"expected_version": version})
