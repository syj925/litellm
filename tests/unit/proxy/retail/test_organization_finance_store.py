from datetime import datetime, timedelta, timezone
from typing import Final

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from litellm.proxy.retail.organization_finance_store import (
    BillingOrganizationUpdate,
    _Assignment,
    _ConsumptionRow,
    assignment_view,
    consumption_item,
    consumption_window,
)

NOW: Final = datetime(2026, 1, 12, 9, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("version", [-1, "1", True, 1.5, None])
def test_assignment_requires_strict_nonnegative_version(version: object) -> None:
    with pytest.raises(ValidationError):
        BillingOrganizationUpdate.model_validate({"organization_id": None, "expected_version": version})


@pytest.mark.parametrize("identifier", ["", " org", "org ", "org\nprivate", "x" * 192])
def test_assignment_rejects_invalid_identifier_without_normalizing_it(identifier: str) -> None:
    with pytest.raises(ValidationError):
        BillingOrganizationUpdate(organization_id=identifier, expected_version=0)


def test_assignment_null_is_explicit_clear_not_omitted_choice() -> None:
    assert BillingOrganizationUpdate(organization_id=None, expected_version=0).model_dump() == {
        "organization_id": None, "expected_version": 0
    }
    with pytest.raises(ValidationError):
        BillingOrganizationUpdate.model_validate({"expected_version": 0})


@pytest.mark.parametrize("field", ["user_id", "enabled", "source", "role", "created_at"])
def test_assignment_rejects_unowned_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        BillingOrganizationUpdate.model_validate({"organization_id": "org-a", "expected_version": 0, field: "private"})


@pytest.mark.parametrize(
    "organization_id,organization_enabled,membership_enabled,state",
    [(None, None, None, "unassigned"), ("org", True, True, "active"),
     ("org", False, True, "organization_disabled"), ("org", True, False, "membership_inactive"),
     ("org", True, None, "membership_inactive"), ("org", False, False, "organization_disabled")],
)
def test_current_assignment_reflects_current_membership_without_changing_choice(
    organization_id: str | None, organization_enabled: bool | None, membership_enabled: bool | None, state: str
) -> None:
    row: Final = _Assignment(
        user_id="owner", organization_id=organization_id, organization_name="Organization" if organization_id else None,
        organization_enabled=organization_enabled, membership_enabled=membership_enabled, version=3,
        updated_at=NOW, updated_by="admin",
    )
    view: Final = assignment_view(row)
    assert view.state == state
    assert view.effective is (state == "active")
    assert view.organization_id == organization_id
    assert view.version == 3


def test_default_window_uses_injected_observation_time() -> None:
    assert consumption_window(None, None, NOW) == (NOW - timedelta(days=7), NOW)


def test_aware_window_normalizes_to_utc_at_full_31_day_boundary() -> None:
    begin: Final = NOW.astimezone(timezone(timedelta(hours=8)))
    finish: Final = begin + timedelta(days=31)
    assert consumption_window(begin, finish, NOW) == (NOW, NOW + timedelta(days=31))


@pytest.mark.parametrize(
    "begin,finish",
    [(None, NOW), (NOW, None), (NOW, NOW), (NOW + timedelta(seconds=1), NOW),
     (NOW, NOW + timedelta(days=31, microseconds=1)), (NOW.replace(tzinfo=None), NOW),
     (NOW, NOW.replace(tzinfo=None))],
)
def test_consumption_window_rejects_ambiguous_or_unbounded_time_ranges(
    begin: datetime | None, finish: datetime | None
) -> None:
    with pytest.raises(HTTPException) as failure:
        consumption_window(begin, finish, NOW)
    assert failure.value.status_code == 422


@pytest.mark.parametrize("nanos", [0, -1, -123456789, -9000000000000000000])
def test_settled_projection_retains_exact_money_and_no_raw_nanos(nanos: int) -> None:
    row: Final = _ConsumptionRow(
        request_id="request", user_id="owner", model="public-model", captured_at=NOW, state="settled",
        charge_nanos=nanos, assignment_version=2, organization_version=3, membership_version=4,
    )
    item: Final = consumption_item(row)
    whole, fraction = divmod(-nanos, 1000000000)
    assert item.settled_usd == f"{whole}.{fraction:09d}"
    assert item.model_dump() == {
        "request_id": "request", "user_id": "owner", "model": "public-model", "captured_at": NOW,
        "state": "settled", "settled_usd": f"{whole}.{fraction:09d}",
        "assignment_version": 2, "organization_version": 3, "membership_version": 4,
    }


@pytest.mark.parametrize("state", ["pending", "unknown", "conflict", "released"])
def test_unsettled_projection_keeps_amount_unknown_instead_of_zero(state: str) -> None:
    row: Final = _ConsumptionRow.model_validate({
        "request_id": "request", "user_id": "owner", "model": "public-model", "captured_at": NOW,
        "state": state, "charge_nanos": None, "assignment_version": 1, "organization_version": 1,
        "membership_version": 1,
    })
    assert consumption_item(row).settled_usd is None
