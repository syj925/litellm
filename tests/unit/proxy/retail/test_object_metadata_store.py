from datetime import datetime, timezone
from typing import Final

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from litellm.proxy.retail.control_plane_access import ControlPlaneActor
from litellm.proxy.retail.object_metadata_store import MetadataUpdate, ObjectMetadataStore, scope_fingerprint
from litellm.proxy.retail.object_metadata_targets import NativeTarget
from litellm.proxy.retail.store import WalletStore


@pytest.mark.parametrize(
    "tags",
    (
        ("",),
        ("Upper",),
        ("has_space",),
        ("two--hyphens",),
        ("-leading",),
        ("trailing-",),
        ("leading-",),
        ("非ascii",),
        ("a" * 41,),
        ("owner", "owner"),
        tuple("label-" + str(index) for index in range(11)),
        ("abcdef12-abcd-abcd-abcd-abcdef123456",),
        ("abcdef1234567890abcdef",),
        ("request-1234567890123456",),
    ),
)
def test_labels_reject_duplicates_unbounded_values_and_obvious_request_identifiers(tags: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        MetadataUpdate(tags=tags, expected_version=0)


def test_label_replacement_is_canonical_and_empty_replacement_clears_labels() -> None:
    data: Final = MetadataUpdate(tags=("production", "owner-a", "eu1"), expected_version=0)
    assert data.tags == ("eu1", "owner-a", "production")
    assert MetadataUpdate(tags=(), expected_version=1).tags == ()


@pytest.mark.parametrize("version", (-1, True, "0"))
def test_cas_version_requires_a_nonnegative_integer(version: object) -> None:
    with pytest.raises(ValidationError):
        MetadataUpdate.model_validate({"tags": [], "expected_version": version})


@pytest.mark.parametrize("field", ("source", "created_by", "updated_by", "native_id", "notes", "metadata"))
def test_clients_cannot_forge_provenance_or_native_binding_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        MetadataUpdate.model_validate({"tags": [], "expected_version": 0, field: "client-input"})


def test_key_ownership_transfer_changes_scope_but_ordinary_updates_do_not() -> None:
    target: Final = NativeTarget(
        native_id="private-native-reference",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        owner_id="owner-a",
        team_id="team-a",
    )
    assert scope_fingerprint("key", target) != scope_fingerprint(
        "key", target.model_copy(update={"owner_id": "owner-b"})
    )
    assert scope_fingerprint("key", target) != scope_fingerprint("key", target.model_copy(update={"team_id": "team-b"}))
    assert scope_fingerprint("key", target) == scope_fingerprint("key", target.model_copy(update={"name": "Renamed"}))
    assert scope_fingerprint("model", target) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ("internal_user", "proxy_admin_viewer", "organization_admin"))
async def test_non_proxy_admin_is_rejected_before_metadata_storage(role: str) -> None:
    store: Final = WalletStore("postgresql://unreachable/unused")
    actor: Final = ControlPlaneActor("account", role, is_admin=False, can_write=role != "proxy_admin_viewer")
    try:
        with pytest.raises(HTTPException) as denied:
            await ObjectMetadataStore(store).targets("key", actor)
        assert denied.value.status_code == 403
    finally:
        await store.aclose()
