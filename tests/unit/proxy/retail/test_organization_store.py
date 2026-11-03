from collections.abc import Mapping
from typing import Final

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.organization_store import MemberUpdate, OrganizationCreate


def test_org_creation_normalizes_display_name_and_known_account_id() -> None:
    record: Final = OrganizationCreate(name="  Example  ", admin_user_id="  account  ")
    assert record.model_dump() == {"name": "Example", "admin_user_id": "account"}


@pytest.mark.parametrize("name", (" ", "New\nName", "\x00Name", "A" * 101))
def test_org_creation_rejects_empty_controls_or_unbounded_names(name: str) -> None:
    with pytest.raises(ValidationError):
        OrganizationCreate(name=name, admin_user_id="account")


@pytest.mark.parametrize(
    "payload",
    (
        {"role": "proxy_admin", "enabled": True, "expected_version": 1, "expected_organization_version": 1},
        {"role": "member", "enabled": "false", "expected_version": 1, "expected_organization_version": 1},
        {"role": "member", "enabled": True, "expected_version": True, "expected_organization_version": 1},
        {"role": "member", "enabled": True, "expected_version": 0, "expected_organization_version": 1},
        {"role": "member", "enabled": True, "expected_version": 1, "expected_organization_version": 0},
    ),
)
def test_membership_changes_require_owned_roles_explicit_booleans_and_positive_versions(
    payload: Mapping[str, str | bool | int],
) -> None:
    with pytest.raises(ValidationError):
        MemberUpdate.model_validate(payload)
