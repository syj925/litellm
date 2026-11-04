from datetime import datetime, timedelta, timezone
from decimal import localcontext
from typing import Final
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from litellm.proxy.retail.budget_management import (
    CreateModelBudget,
    ScheduleModelBudget,
    UpdateModelBudget,
    _Policy,
    budget_view,
    exact_model_name,
    money_string,
    pagination,
)
from litellm.proxy.retail.models import MAX_AMOUNT_NANOS

NOW: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "amount",
    ("0", "-1", "NaN", "Infinity", "1e3", "01", ".1", "1.", " 1", "1.0000000001", "9000000000.000000001", "9999999999"),
)
def test_budget_money_rejects_nondecimal_nonpositive_or_overflow_limits(amount: str) -> None:
    with pytest.raises(ValidationError):
        CreateModelBudget(model="public-model", ends_at=NOW, limit_usd=amount)


def test_budget_money_keeps_nanos_and_accepts_the_entire_financial_limit() -> None:
    smallest: Final = CreateModelBudget(model="public-model", ends_at=NOW, limit_usd="0.000000001")
    largest: Final = CreateModelBudget(model="public-model", ends_at=NOW, limit_usd=money_string(MAX_AMOUNT_NANOS))
    assert smallest.limit_usd == "0.000000001"
    assert largest.limit_usd == "9000000000.000000000"
    assert CreateModelBudget(model="public-model", ends_at=NOW, limit_usd="1.2").limit_usd == "1.200000000"
    with localcontext() as context:
        context.prec = 4
        assert money_string(99999999999999999999999999999999999999) == "99999999999999999999999999999.999999999"


@pytest.mark.parametrize(
    "model", ("", " public-model", "public-model ", "*", "model?", "model[1]", "a\x00b", "a" * 192)
)
def test_exact_model_names_reject_patterns_and_unbounded_inputs(model: str) -> None:
    assert exact_model_name(model) is False
    with pytest.raises(ValidationError):
        CreateModelBudget(model=model, ends_at=NOW, limit_usd="1")


def test_configured_public_aliases_may_contain_a_provider_prefix() -> None:
    assert CreateModelBudget(model="vendor/public-alias", ends_at=NOW, limit_usd="1").model == "vendor/public-alias"


@pytest.mark.parametrize(
    "data",
    (
        {"expected_version": 1},
        {"limit_usd": None, "expected_version": 1},
        {"enabled": None, "expected_version": 1},
        {"enabled": True, "expected_version": True},
        {"enabled": "false", "expected_version": 1},
        {"enabled": False, "expected_version": 0},
        {"enabled": False, "expected_version": "1"},
        {"enabled": True, "expected_version": 1, "ends_at": "forged"},
    ),
)
def test_budget_updates_require_a_strict_config_cas_and_actual_editable_fields(data: object) -> None:
    with pytest.raises(ValidationError):
        UpdateModelBudget.model_validate(data)


def test_creation_requires_a_timezone_and_does_not_accept_client_provenance() -> None:
    with pytest.raises(ValidationError):
        CreateModelBudget.model_validate({"model": "public-model", "ends_at": "2026-01-01T00:00:00", "limit_usd": "1"})
    with pytest.raises(ValidationError):
        CreateModelBudget.model_validate(
            {"model": "public-model", "ends_at": NOW, "limit_usd": "1", "created_by": "forged"}
        )
    data: Final = CreateModelBudget(
        model="public-model", ends_at=NOW.astimezone(timezone(timedelta(hours=8))), limit_usd="1"
    )
    assert data.ends_at == NOW and data.ends_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "extra",
    (
        {"expected_version": 0},
        {"expected_version": True},
        {"expected_version": "1"},
        {"expected_version": 1.0},
        {"expected_version": 9_223_372_036_854_775_807},
        {"enabled": "false"},
        {"enabled": None},
        {"limit_usd": "0.0000000001"},
        {"limit_usd": "NaN"},
        {"limit_usd": 1},
        {"ends_at": "2026-02-01T00:00:00"},
        {"model": "forged-model"},
        {"effective_from": NOW},
        {"predecessor_id": str(UUID(int=2))},
        {"created_by": "forged-actor"},
    ),
)
def test_successor_requires_strict_cas_and_rejects_client_window_identity(extra: object) -> None:
    assert isinstance(extra, dict)
    with pytest.raises(ValidationError):
        ScheduleModelBudget.model_validate(
            {"expected_version": 1, "ends_at": NOW, "limit_usd": "1", **extra}
        )


def test_successor_normalizes_money_and_offset_without_accepting_a_start_or_model() -> None:
    selected: Final = ScheduleModelBudget(
        expected_version=3,
        ends_at=NOW.astimezone(timezone(timedelta(hours=8))),
        limit_usd="0.000000001",
        enabled=False,
    )
    assert selected.model_dump(mode="json") == {
        "expected_version": 3,
        "ends_at": "2026-01-01T00:00:00Z",
        "limit_usd": "0.000000001",
        "enabled": False,
    }


@pytest.mark.parametrize(
    ("enabled", "expired", "state"),
    ((True, False, "active"), (False, False, "disabled"), (True, True, "expired"), (False, True, "expired")),
)
def test_public_projection_has_exact_overrun_and_no_admin_provenance(enabled: bool, expired: bool, state: str) -> None:
    row: Final = _Policy(
        id=UUID(int=1),
        user_id="private-account",
        model="public-model",
        effective_from=NOW,
        ends_at=NOW + timedelta(hours=1),
        limit_nanos=1000000000,
        spent_nanos=800000001,
        held_nanos=500000001,
        enabled=enabled,
        config_version=3,
        accounting_version=8,
        source="management_api",
        created_at=NOW,
        created_by="private-creator",
        updated_at=NOW,
        updated_by="private-editor",
        accounted_at=NOW,
        observed_at=NOW + timedelta(hours=2) if expired else NOW,
    )
    public: Final = budget_view(row, admin=False)
    admin: Final = budget_view(row, admin=True)
    assert (public["state"], public["remaining_usd"], public["overrun_usd"]) == (state, "0.000000000", "0.300000002")
    assert public["spent_usd"] == "0.800000001" and public["held_usd"] == "0.500000001"
    assert (public["config_version"], public["accounting_version"]) == (3, 8)
    assert set(admin) - set(public) == {"user_id", "source", "created_at", "created_by", "updated_at", "updated_by"}
    assert all("private-" not in str(value) for value in public.values())


@pytest.mark.parametrize(
    ("enabled", "observed_at", "state"),
    (
        (True, NOW - timedelta(microseconds=1), "scheduled"),
        (False, NOW - timedelta(microseconds=1), "scheduled"),
        (True, NOW, "active"),
        (False, NOW, "disabled"),
        (True, NOW + timedelta(hours=1), "expired"),
        (False, NOW + timedelta(hours=1), "expired"),
    ),
)
def test_successor_projection_has_half_open_state_boundaries_and_preserves_own_links(
    enabled: bool, observed_at: datetime, state: str
) -> None:
    row: Final = _Policy(
        id=UUID(int=2),
        user_id="private-owner",
        model="public-model",
        predecessor_id=UUID(int=1),
        successor_id=UUID(int=3),
        effective_from=NOW,
        ends_at=NOW + timedelta(hours=1),
        limit_nanos=1_000_000_000,
        spent_nanos=0,
        held_nanos=0,
        enabled=enabled,
        config_version=1,
        accounting_version=0,
        source="management_api",
        created_at=NOW - timedelta(days=1),
        created_by="private-creator",
        updated_at=NOW - timedelta(days=1),
        updated_by="private-editor",
        accounted_at=None,
        observed_at=observed_at,
    )
    public: Final = budget_view(row, admin=False)
    assert public == {
        "id": str(UUID(int=2)),
        "model": "public-model",
        "effective_from": "2026-01-01T00:00:00Z",
        "ends_at": "2026-01-01T01:00:00Z",
        "limit_usd": "1.000000000",
        "spent_usd": "0.000000000",
        "held_usd": "0.000000000",
        "remaining_usd": "1.000000000",
        "overrun_usd": "0.000000000",
        "enabled": enabled,
        "config_version": 1,
        "accounting_version": 0,
        "accounted_at": None,
        "predecessor_id": str(UUID(int=1)),
        "successor_id": str(UUID(int=3)),
        "recurrence_id": None,
        "state": state,
    }


@pytest.mark.parametrize(
    ("q", "limit", "offset"),
    (("x" * 101, 25, 0), ("\x00", 25, 0), ("", 0, 0), ("", 101, 0), ("", 25, -1), ("", 25, 10001)),
)
def test_management_pagination_has_fixed_server_bounds(q: str, limit: int, offset: int) -> None:
    with pytest.raises(HTTPException) as failure:
        pagination(q, limit, offset)
    assert failure.value.status_code == 422
