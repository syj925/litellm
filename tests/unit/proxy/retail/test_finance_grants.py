from typing import Final
from uuid import uuid4

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.finance_grants import FinanceAuthority, FinanceGrantUpdate


@pytest.mark.parametrize("field", ("can_view", "can_export"))
@pytest.mark.parametrize("value", ("false", "true", 0, 1, None))
def test_finance_grants_accept_only_real_boolean_permissions(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        FinanceGrantUpdate.model_validate({"can_view": True, "can_export": False, "expected_version": 0, field: value})


def test_export_requires_explicit_view_and_cas_version_is_a_strict_integer() -> None:
    with pytest.raises(ValidationError):
        FinanceGrantUpdate(can_view=False, can_export=True, expected_version=0)
    with pytest.raises(ValidationError):
        FinanceGrantUpdate.model_validate({"can_view": True, "can_export": True, "expected_version": "1"})
    revoked: Final = FinanceGrantUpdate(can_view=False, can_export=False, expected_version=0)
    assert revoked.can_view is False and revoked.can_export is False


def test_authorization_epoch_ignores_rename_but_matches_every_capability_generation() -> None:
    authority: Final = FinanceAuthority(grant_id=uuid4(), version=1, revision=uuid4(), member_version=3,
                                       org_generation=2, organization_id="org", organization_name="Before", user_id="member", can_export=True)
    assert authority.same_epoch(authority.model_copy(update={"organization_name": "After"}))
    for changes in ({"grant_id": uuid4()}, {"version": 2}, {"revision": uuid4()}, {"member_version": 4},
                    {"org_generation": 3}, {"organization_id": "other"}, {"user_id": "other"}):
        assert not authority.same_epoch(authority.model_copy(update=changes))
