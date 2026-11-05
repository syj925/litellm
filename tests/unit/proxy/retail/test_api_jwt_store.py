from typing import Final
from uuid import uuid4

import pytest

from litellm.proxy._types import hash_token
from litellm.proxy.retail.api_jwt_config import ApiJwtBinding
from litellm.proxy.retail.api_jwt_store import _eligible, _Target


def binding() -> ApiJwtBinding:
    return ApiJwtBinding(subject="fixture-subject", user_id="fixture-owner",
        token_hash=hash_token("fixture-key"), binding_revision=uuid4())


def target() -> _Target:
    return _Target(
        key_data={"token": hash_token("fixture-key"), "user_id": "fixture-owner", "key_type": "llm_api",
            "models": ["fixture-model"], "metadata": {"retail_resource_policy": {"mode": "auto"}}},
        user_data={"user_id": "fixture-owner", "user_role": "customer", "models": ["fixture-model"]},
        active=True,
    )


@pytest.mark.parametrize("role", ("customer", "internal_user"))
@pytest.mark.parametrize("key_type", (None, "llm_api"))
def test_ordinary_targets_accept_only_native_supported_owners_and_model_routes(role: str, key_type: str | None) -> None:
    sample: Final = target()
    candidate: Final = sample.model_copy(update={
        "user_data": {**sample.user_data, "user_role": role},
        "key_data": {**sample.key_data, "key_type": key_type,
            "allowed_routes": ["llm_api_routes", "/v1/chat/completions"]},
    })
    assert _eligible(candidate, binding()) is True


@pytest.mark.parametrize("role", ("proxy_admin", "proxy_admin_viewer", "internal_user_viewer", "org_admin", "", None))
def test_native_privileged_viewer_missing_and_unknown_roles_cannot_be_mapped(role: str | None) -> None:
    sample: Final = target()
    candidate: Final = sample.model_copy(update={"user_data": {**sample.user_data, "user_role": role}})
    assert _eligible(candidate, binding()) is False


@pytest.mark.parametrize("key_type", ("management", "read_only", "invalid"))
def test_non_model_key_types_are_rejected(key_type: str) -> None:
    sample: Final = target()
    assert _eligible(sample.model_copy(update={"key_data": {**sample.key_data, "key_type": key_type}}), binding()) is False


@pytest.mark.parametrize("routes", (["management_routes"], ["info_routes"], ["/retail/*"], ["*"], ["/key/info"],
    ["/v1/chat/completions", "/key/generate"], "llm_api_routes", [1]))
def test_legacy_or_mislabeled_keys_cannot_bring_management_grants(routes: object) -> None:
    sample: Final = target()
    candidate: Final = _Target.model_validate({**sample.model_dump(),
        "key_data": {**sample.key_data, "allowed_routes": routes}})
    assert _eligible(candidate, binding()) is False


@pytest.mark.parametrize("owner", ("default_user_id", "litellm_internal_jobs", "litellm-internal-health-check",
    "__db_unavailable_fallback__"))
def test_reserved_native_identities_are_rejected_even_if_role_was_changed(owner: str) -> None:
    sample: Final = target()
    candidate: Final = sample.model_copy(update={"key_data": {**sample.key_data, "user_id": owner},
        "user_data": {**sample.user_data, "user_id": owner}})
    assert _eligible(candidate, binding().model_copy(update={"user_id": owner})) is False


@pytest.mark.parametrize("field", ("team_id", "organization_id", "project_id", "object_permission_id", "agent_id"))
def test_target_graph_links_are_rejected(field: str) -> None:
    sample: Final = target()
    assert _eligible(sample.model_copy(update={"key_data": {**sample.key_data, field: "linked"}}), binding()) is False


@pytest.mark.parametrize("metadata", ({"login_method": "retail_oidc"}, {"service_account_id": "service-account"},
    {"managed_agent": True}, {"service_account": True}))
def test_session_and_service_account_metadata_never_becomes_an_inference_identity(metadata: object) -> None:
    sample: Final = target()
    candidate: Final = _Target.model_validate({**sample.model_dump(), "key_data": {**sample.key_data, "metadata": metadata}})
    assert _eligible(candidate, binding()) is False


def test_owner_and_exact_key_hash_must_both_match_configuration() -> None:
    sample: Final = target()
    assert _eligible(sample, binding().model_copy(update={"user_id": "other-owner"})) is False
    assert _eligible(sample, binding().model_copy(update={"token_hash": "b" * 64})) is False
    assert _eligible(sample.model_copy(update={"active": False}), binding()) is False


def test_reset_and_explicit_scim_deactivation_are_rejected() -> None:
    sample: Final = target()
    reset: Final = sample.model_copy(update={"user_data": {**sample.user_data, "password_reset_required": True}})
    scim: Final = sample.model_copy(update={"user_data": {**sample.user_data, "metadata": {"scim_active": False}}})
    assert _eligible(reset, binding()) is False
    assert _eligible(scim, binding()) is False
