from datetime import datetime, timezone
from typing import Final

import pytest
from pydantic import JsonValue, SecretStr, TypeAdapter, ValidationError

from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_if_encrypted_with, encrypt_value_helper
from litellm.proxy.common_utils.validation_error_body import public_validation_errors
from litellm.proxy.retail.channel_connection import (
    ChannelConnectionPatch,
    _EncryptedModel,
    connection_changes,
    connection_changes_are_live,
    connection_summary,
    valid_api_base,
)
from litellm.proxy.retail.channel_store import ChannelMember, ChannelRow
from litellm.proxy.retail.channels import ChannelEdit

_NOW: Final = datetime(2026, 1, 2, tzinfo=timezone.utc)
_TEST_SALT: Final = "unit-test-encryption-material"
_STRING: Final = TypeAdapter(str)


def _row(identifier: str, **values: JsonValue) -> _EncryptedModel:
    return _EncryptedModel(
        model_id=identifier,
        updated_by="admin",
        updated_at=_NOW,
        litellm_params={
            "model": "anthropic/upstream",
            "api_base": "https://old.invalid/v1",
            "api_key": "old-test-key",
            "input_cost_per_token": 0.00001,
            "tpm": 77,
            "rpm": 22,
            "order": 2,
            "weight": 9,
            "custom_llm_provider": "anthropic",
            **values,
        },
    )


def _channel() -> ChannelRow:
    return ChannelRow(id="channel", name="Channel", pool_id="pool", status="active", version=3, updated_at=_NOW)


def _members(*identifiers: str) -> tuple[ChannelMember, ...]:
    return tuple(ChannelMember(channel_id="channel", deployment_id=item, model="public-model") for item in identifiers)


def _encrypt(value: str) -> str:
    return _STRING.validate_python(encrypt_value_helper(value, new_encryption_key=_TEST_SALT))


@pytest.mark.parametrize(
    "url",
    [
        "https://upstream.invalid",
        "https://upstream.invalid/v1/",
        "http://127.0.0.1:18000",
        "http://localhost:8080/custom/anthropic",
        "http://[::1]:4001/api",
    ],
)
def test_custom_api_base_preserves_exact_protocol_path_and_trailing_slash(url: str) -> None:
    assert valid_api_base(url) == url
    assert ChannelConnectionPatch(api_base=url).api_base == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host.invalid",
        "https://user:private@host.invalid",
        "https://user@host.invalid",
        "https://host.invalid?key=private",
        "https://host.invalid/path#private",
        "https://host.invalid?",
        "https://host.invalid#",
        "https://host.invalid:99999",
        "http://host.invalid:0",
        "/v1",
        "https://",
        "https://host.invalid/path with space",
        "https://host.invalid\\private",
        "https://host.invalid/\r\nX:secret",
    ],
)
def test_api_base_rejects_ambiguous_urls_and_embedded_credentials(url: str) -> None:
    with pytest.raises(ValidationError):
        ChannelConnectionPatch(api_base=url)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"api_key": None},
        {"api_key": ""},
        {"api_key": "  "},
        {"api_base": None},
        {"api_base": ""},
        {"api_base": "  "},
        {"api_key": "", "api_base": ""},
    ],
)
def test_empty_connection_values_never_clear_existing_credentials_or_urls(values: dict[str, JsonValue]) -> None:
    patch: Final = ChannelConnectionPatch.model_validate(values)
    original: Final = _row("one")
    changes: Final = connection_changes((original,), patch, _encrypt)
    assert patch.changed is False
    assert changes[0].params == original.litellm_params
    assert changes[0].changed_fields == ()


def test_url_only_bulk_change_keeps_each_key_reference_and_every_unrelated_parameter() -> None:
    rows: Final = (_row("one", api_key="os.environ/FIRST_TEST_KEY"), _row("two", api_key="second-test-key"))
    patch: Final = ChannelConnectionPatch(api_base="https://new.invalid/custom")
    changes: Final = connection_changes(rows, patch, _encrypt)
    assert [item.original.model_id for item in changes] == ["one", "two"]
    for change in changes:
        assert change.changed_fields == ("api_base",)
        assert change.params["api_key"] == change.original.litellm_params["api_key"]
        assert {key: value for key, value in change.params.items() if key != "api_base"} == {
            key: value for key, value in change.original.litellm_params.items() if key != "api_base"
        }
        assert decrypt_if_encrypted_with(str(change.params["api_base"]), _TEST_SALT) == patch.api_base
        assert change.params["api_base"] != patch.api_base


def test_key_only_bulk_change_encrypts_new_key_and_preserves_distinct_urls() -> None:
    rows: Final = (_row("one"), _row("two", api_base="https://other.invalid"))
    changes: Final = connection_changes(rows, ChannelConnectionPatch(api_key="new-test-key"), _encrypt)
    for change in changes:
        assert change.changed_fields == ("api_key",)
        assert change.params["api_base"] == change.original.litellm_params["api_base"]
        assert decrypt_if_encrypted_with(str(change.params["api_key"]), _TEST_SALT) == "new-test-key"
        assert change.params["api_key"] != "new-test-key"
        assert change.original.litellm_params["api_key"] == "old-test-key"


def test_safe_summary_never_returns_existing_secret_or_named_credential_name() -> None:
    summary: Final = connection_summary(
        _channel(),
        _members("one", "two"),
        (
            _row("one", litellm_credential_name="private-credential-name"),
            _row("two"),
        ),
    )
    payload: Final = summary.model_dump_json()
    assert "old-test-key" not in payload
    assert "private-credential-name" not in payload
    assert summary.can_edit is False
    assert summary.credential_mode == "mixed"
    assert summary.editable_deployment_count == 1
    assert summary.has_api_key is True
    assert summary.api_base == "https://old.invalid/v1"


def test_summary_distinguishes_mixed_urls_and_missing_keys_without_exposing_any_secret() -> None:
    summary: Final = connection_summary(
        _channel(),
        _members("one", "two"),
        (_row("one"), _row("two", api_base="https://second.invalid/custom", api_key=None)),
    )
    assert summary.can_edit is True
    assert summary.api_base is None
    assert summary.api_base_mixed is True
    assert summary.has_api_key is None
    assert summary.version == 3


def test_static_or_missing_deployment_blocks_bulk_change_instead_of_partial_update() -> None:
    summary: Final = connection_summary(_channel(), _members("one", "static"), (_row("one"),))
    assert summary.deployment_count == 2
    assert summary.editable_deployment_count == 1
    assert summary.can_edit is False
    assert summary.reason is not None and "read-only" in summary.reason
    assert summary.has_api_key is None


@pytest.mark.parametrize(
    "field", ["aws_access_key_id", "aws_profile_name", "vertex_credentials", "azure_ad_token", "refresh_token"]
)
def test_non_api_key_authentication_is_read_only_without_echoing_credentials(field: str) -> None:
    summary: Final = connection_summary(
        _channel(), _members("one"), (_row("one", api_key=None, **{field: "private-native-credential"}),)
    )
    assert summary.can_edit is False
    assert summary.reason is not None and "native cloud" in summary.reason
    assert "private-native-credential" not in summary.model_dump_json()


def test_existing_api_key_remains_editable_when_native_provider_metadata_is_present() -> None:
    summary: Final = connection_summary(
        _channel(), _members("one"), (_row("one", vertex_credentials="native-provider-metadata"),)
    )
    assert summary.can_edit is True
    assert summary.reason is None


def test_secret_in_old_api_base_userinfo_is_never_returned() -> None:
    summary: Final = connection_summary(
        _channel(), _members("one"), (_row("one", api_base="https://user:old-private-password@host.invalid"),)
    )
    assert summary.api_base is None
    assert "old-private-password" not in summary.model_dump_json()
    assert summary.can_edit is False
    assert summary.reason is not None and "existing API base" in summary.reason


def test_reconcile_requires_actual_new_params_not_only_matching_model_ids() -> None:
    row: Final = _row("one")
    changes: Final = connection_changes(
        (row,), ChannelConnectionPatch(api_base="https://new.invalid"), lambda value: value
    )
    old_live: Final = {"one": {"litellm_params": row.litellm_params}}
    new_live: Final = {"one": {"litellm_params": dict(changes[0].params)}}
    assert connection_changes_are_live(changes, old_live) is False
    assert connection_changes_are_live(changes, {}) is False
    assert connection_changes_are_live(changes, new_live) is True
    assert connection_changes_are_live(changes, old_live, restored=True) is True


def test_secretstr_runtime_keys_are_compared_without_returning_their_values() -> None:
    changes: Final = connection_changes(
        (_row("one"),), ChannelConnectionPatch(api_key="replacement-test-key"), lambda value: value
    )
    live: Final = {"one": {"litellm_params": {"api_key": SecretStr("replacement-test-key")}}}
    assert connection_changes_are_live(changes, live) is True
    assert (
        connection_changes_are_live(changes, {"one": {"litellm_params": {"api_key": SecretStr("wrong-key")}}}) is False
    )


@pytest.mark.parametrize(
    "values",
    [
        {"api_key": "sensitive-test-secret\r\nX:private"},
        {"api_base": "https://sensitive-test-secret@host.invalid"},
    ],
)
def test_validation_error_public_projection_does_not_echo_connection_inputs(values: dict[str, str]) -> None:
    with pytest.raises(ValidationError) as error:
        ChannelConnectionPatch.model_validate(values)
    assert "sensitive-test-secret" not in str(error.value)
    public: Final = public_validation_errors(error.value.errors())
    assert "sensitive-test-secret" not in str(public)
    assert all(set(item) == {"type", "loc", "msg"} for item in public)


def test_channel_edit_keeps_new_key_secret_and_rejects_unsupported_connection_fields() -> None:
    edit: Final = ChannelEdit(
        name="Channel",
        pool_id="pool",
        deployment_ids=("one",),
        expected_version=3,
        connection_patch=ChannelConnectionPatch(api_key="replacement-test-key"),
    )
    assert "replacement-test-key" not in repr(edit)
    assert "replacement-test-key" not in edit.model_dump_json()
    with pytest.raises(ValidationError):
        ChannelConnectionPatch.model_validate({"litellm_credential_name": "shared"})
