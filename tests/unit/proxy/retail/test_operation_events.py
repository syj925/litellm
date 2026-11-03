from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Final

import pytest
from fastapi import HTTPException
from pydantic import JsonValue

from litellm.proxy.retail.operation_events import event_window, operation_digest, safe_changes


def test_idempotency_digest_is_canonical_and_bound_to_action_and_payload() -> None:
    initial: Final = operation_digest("organization.updated", {"name": "A", "enabled": True})
    assert operation_digest("organization.updated", {"enabled": True, "name": "A"}) == initial
    assert operation_digest("organization.updated", {"enabled": False, "name": "A"}) != initial
    assert operation_digest("organization.created", {"enabled": True, "name": "A"}) != initial


@pytest.mark.parametrize("changes", ({"api_key": "private"}, {"name": {"token": "private"}}, {"name": ["private"]}))
def test_audit_payload_rejects_credentials_and_nested_unsanitized_values(changes: Mapping[str, JsonValue]) -> None:
    with pytest.raises(ValueError):
        safe_changes(changes)


def test_audit_changes_preserve_allowed_scalar_changes_without_dropping_fields() -> None:
    assert safe_changes({"role": "member", "enabled": False, "user_id": "account"}) == {
        "role": "member",
        "enabled": False,
        "user_id": "account",
    }


@pytest.mark.parametrize("days", (0, -1, 32))
def test_audit_windows_bound_work_and_require_positive_ranges(days: int) -> None:
    start: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(HTTPException) as denied:
        event_window(start, start + timedelta(days=days))
    assert denied.value.status_code == 422


def test_audit_windows_reject_partial_or_ambiguous_timezones() -> None:
    start: Final = datetime.fromisoformat("2026-01-01T00:00:00")
    with pytest.raises(HTTPException) as partial:
        event_window(start, None)
    with pytest.raises(HTTPException) as ambiguous:
        event_window(start, start + timedelta(days=1))
    assert (partial.value.status_code, ambiguous.value.status_code) == (422, 422)
