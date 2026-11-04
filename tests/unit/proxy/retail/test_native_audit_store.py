from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import UUID

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.native_audit_store import NativeAuditEvent, NativeAuditMetadata, append_native_audit


@dataclass(frozen=True, slots=True)
class UnavailableNativeWriter:
    async def query_raw(self, query: str, *args: object) -> object:
        raise AssertionError("A disabled audit feature must not borrow a native connection")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_enabled,native_enabled", (("false", "true"), ("true", "false"), ("false", "false")))
async def test_disabled_audit_never_queries_native_storage(
    monkeypatch: pytest.MonkeyPatch, operation_enabled: str, native_enabled: str,
) -> None:
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", operation_enabled)
    monkeypatch.setenv("RETAIL_NATIVE_AUDIT_ENABLED", native_enabled)
    assert await append_native_audit(UnavailableNativeWriter(), actor_id="owner", action="native_key.rotated") is None


@pytest.mark.parametrize("value", (True, "1", 1.5, -1, 9_223_372_036_854_775_808))
def test_metadata_rejects_ambiguous_or_invalid_affected_counts(value: object) -> None:
    with pytest.raises(ValidationError):
        NativeAuditMetadata.model_validate({"actor_id": "owner", "action": "native_key.rotated", "affected_count": value})


@pytest.mark.parametrize("actor", ("", " owner", "owner ", "owner\nother", "x" * 192))
def test_metadata_rejects_noncanonical_actor_identity(actor: str) -> None:
    with pytest.raises(ValidationError):
        NativeAuditMetadata(actor_id=actor, action="native_key.rotated", affected_count=1)


@pytest.mark.parametrize("field", ("token", "key", "password", "request_body", "credential_hash"))
def test_metadata_cannot_accept_credential_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        NativeAuditMetadata.model_validate({
            "actor_id": "owner", "action": "native_key.rotated", "affected_count": 1, field: "fixture-private-value",
        })


def test_replay_digest_uses_source_identity_metadata_and_absolute_occurrence_time() -> None:
    event: Final = NativeAuditEvent(
        id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        source_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        actor_id="owner", action="native_key.rotated", resource_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        affected_count=1, occurred_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
    )
    same: Final = event.model_copy(update={"occurred_at": event.occurred_at.astimezone(timezone(timedelta(hours=8)))})
    assert event.digest() == same.digest()
    assert event.operation_key() == same.operation_key()
    assert len(event.operation_key()) <= 128
    changed: Final = event.model_copy(update={"affected_count": 2})
    assert event.digest() != changed.digest()
    other_source: Final = event.model_copy(update={"source_id": UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")})
    assert event.operation_key() != other_source.operation_key()
    assert event.digest() != other_source.digest()
