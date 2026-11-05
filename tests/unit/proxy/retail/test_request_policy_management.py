from collections.abc import Mapping
from typing import Final

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.request_policy_management import BindingUpdate, PolicyCreate, PolicyUpdate, PromptCreate
from litellm.proxy.retail.request_policy_types import Definition


@pytest.mark.parametrize("name", ("", " leading", "trailing ", "line\nname", "\x00", "x"*101))
def test_admin_resource_names_reject_ambiguous_or_unbounded_values(name: str) -> None:
    with pytest.raises(ValidationError):
        PolicyCreate(name=name, definition=Definition())


@pytest.mark.parametrize("content", ("x"*8001, "中"*2667, "before\x00after"))
def test_prompt_limit_is_utf8_and_errors_never_include_prompt(content: str) -> None:
    with pytest.raises(ValidationError) as failure:
        PromptCreate(name="Safe name", content=content)
    assert content not in str(failure.value)


def test_prompt_text_is_private_and_limit_accepts_exact_utf8_boundary() -> None:
    content: Final = "中"*2666+"ab"
    value: Final = PromptCreate(name="UTF8", content=content)
    assert len(value.content.encode()) == 8000 and content not in repr(value)


@pytest.mark.parametrize("version", (True, "1", 0, -1, 9007199254740992))
def test_cas_versions_are_strict_positive_safe_integers(version: object) -> None:
    with pytest.raises(ValidationError):
        PolicyUpdate.model_validate({"name": "Safe", "definition": {}, "expected_version": version})


def test_full_update_can_clear_restrictions_and_prompt_explicitly() -> None:
    value: Final = PolicyUpdate.model_validate({"name": "Cleared", "expected_version": 1,
        "definition": {"temperature": None,"allowed_routes": None},"prompt_id": None})
    assert value.prompt_id is None and value.definition.temperature is None
    assert value.definition.allowed_routes is None
    assert PolicyCreate(name="Deny routes", definition=Definition(allowed_routes=())).definition.allowed_routes == ()


@pytest.mark.parametrize("extra", ({"target_id": "hidden-owner"}, {"native_id": "hidden-key"}, {"expected_version": 1.5}))
def test_binding_update_does_not_accept_identity_reassignment(extra: Mapping[str, object]) -> None:
    with pytest.raises(ValidationError):
        BindingUpdate.model_validate({"policy_id": "00000000-0000-0000-0000-000000000001", "enabled": False,
            "expected_version": 1, **extra})
