from datetime import datetime, timezone
from typing import Final

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from litellm.proxy.retail.channel_store import (
    ChannelDetails,
    ChannelMember,
    ChannelRow,
    channel_view,
    connection_details,
    require_whole_channels,
    selected_members,
)
from litellm.proxy.retail.pool_store import DeploymentRecord, PoolBinding


def _channel(identifier: str, pool: str) -> ChannelRow:
    return ChannelRow(
        id=identifier,
        name=identifier,
        pool_id=pool,
        status="active",
        version=1,
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _deployment(public_model: str, **params: object) -> dict[str, object]:
    return {"model_name": public_model, "litellm_params": {"model": "anthropic/" + public_model, **params}}


def test_connection_groups_models_by_exact_upstream_without_exposing_authentication() -> None:
    first: Final = _deployment(
        "model-a", api_base="https://person:secret@example.invalid/v1?key=private", api_key="key-a"
    )
    second: Final = _deployment(
        "model-b", api_base="https://person:secret@example.invalid/v1?key=private", api_key="key-a"
    )
    identity, host = connection_details(first)
    assert connection_details(second) == (identity, host)
    assert host == "example.invalid"
    assert len(identity) == 64
    assert "key-a" not in identity
    assert (
        connection_details(_deployment("model-a", api_base="https://example.invalid/v1", api_key="key-b"))[0]
        != identity
    )
    assert (
        connection_details(_deployment("model-a", api_base="https://example.invalid/v2", api_key="key-a"))[0]
        != identity
    )


def test_saved_credential_identity_does_not_change_when_its_resolved_secret_rotates() -> None:
    first: Final = _deployment("a", litellm_credential_name="upstream-a", api_key="old")
    rotated: Final = _deployment("b", litellm_credential_name="upstream-a", api_key="new")
    different: Final = _deployment("a", litellm_credential_name="upstream-b", api_key="old")
    assert connection_details(first)[0] == connection_details(rotated)[0]
    assert connection_details(first)[0] != connection_details(different)[0]


def test_wrapped_api_secrets_identify_the_actual_key_instead_of_the_mask() -> None:
    first: Final = _deployment("a", api_key=SecretStr("first-secret"))
    same: Final = _deployment("b", api_key="first-secret")
    different: Final = _deployment("a", api_key=SecretStr("second-secret"))
    assert connection_details(first)[0] == connection_details(same)[0]
    assert connection_details(first)[0] != connection_details(different)[0]


def test_native_aws_connection_identity_includes_authentication_and_region() -> None:
    params: Final = {
        "model": "bedrock/example",
        "aws_access_key_id": "account-a",
        "aws_secret_access_key": SecretStr("private-a"),
        "aws_region_name": "region-a",
    }
    first: Final = _deployment("a", **params)
    same: Final = _deployment("b", **params)
    other_account: Final = _deployment("a", **{**params, "aws_access_key_id": "account-b"})
    other_region: Final = _deployment("a", **{**params, "aws_region_name": "region-b"})
    other_secret: Final = _deployment("a", **{**params, "aws_secret_access_key": SecretStr("private-b")})
    assert connection_details(first)[0] == connection_details(same)[0]
    assert len({connection_details(item)[0] for item in (first, other_account, other_region, other_secret)}) == 4


def test_native_vertex_connection_identity_includes_credentials_project_and_location() -> None:
    params: Final = {
        "model": "vertex_ai/example",
        "vertex_project": "project-a",
        "vertex_location": "location-a",
        "vertex_credentials": {"client_email": "account-a", "private_key": SecretStr("private-a")},
    }
    first: Final = _deployment("a", **params)
    same: Final = _deployment(
        "b", **{**params, "vertex_credentials": {"private_key": SecretStr("private-a"), "client_email": "account-a"}}
    )
    other_project: Final = _deployment("a", **{**params, "vertex_project": "project-b"})
    other_location: Final = _deployment("a", **{**params, "vertex_location": "location-b"})
    other_credentials: Final = _deployment(
        "a", **{**params, "vertex_credentials": {"client_email": "account-b", "private_key": SecretStr("private-b")}}
    )
    assert connection_details(first)[0] == connection_details(same)[0]
    assert len({connection_details(item)[0] for item in (first, other_project, other_location, other_credentials)}) == 4


@pytest.mark.parametrize(
    "params",
    [
        {"model": "bedrock/example", "aws_region_name": "region"},
        {"model": "vertex_ai/example", "vertex_project": "project"},
        {"api_key": SecretStr("**********")},
        {},
    ],
)
def test_unknown_or_masked_authentication_is_never_grouped_by_shared_model_name(params: dict[str, object]) -> None:
    first: Final = {**_deployment("same", **params), "model_info": {"id": "deployment-a"}}
    second: Final = {**_deployment("same", **params), "model_info": {"id": "deployment-b"}}
    assert connection_details(first)[0] != connection_details(second)[0]
    assert connection_details(first)[0] == connection_details(first)[0]


@pytest.mark.parametrize("base", ["http://[invalid", "https://example.invalid:invalid/path", "os.environ/UPSTREAM"])
def test_invalid_or_unresolved_endpoint_does_not_expose_raw_value(base: str) -> None:
    assert connection_details(_deployment("a", api_base=base))[1] is None


def test_named_channel_requires_meaningful_name_and_tracks_exact_deployment_not_model_alias() -> None:
    with pytest.raises(ValidationError):
        ChannelDetails(name="  ", pool_id="a")
    records: Final = (
        DeploymentRecord(deployment_id="one", model="same-model", provider="anthropic"),
        DeploymentRecord(deployment_id="two", model="same-model", provider="anthropic"),
    )
    selected: Final = selected_members("channel", ("two",), records)
    assert selected == (ChannelMember(channel_id="channel", deployment_id="two", model="same-model"),)
    with pytest.raises(HTTPException, match="400"):
        selected_members("channel", ("same-model",), records)


def test_pool_binding_edits_cannot_split_or_copy_a_named_channel() -> None:
    channels: Final = (_channel("channel-a", "pool-a"), _channel("channel-b", "pool-b"))
    members: Final = (
        ChannelMember(channel_id="channel-a", deployment_id="a1", model="same"),
        ChannelMember(channel_id="channel-a", deployment_id="a2", model="other"),
        ChannelMember(channel_id="channel-b", deployment_id="b1", model="same"),
    )
    complete: Final = (
        PoolBinding(deployment_id="a1", model="same"),
        PoolBinding(deployment_id="a2", model="other", weight=3),
    )
    require_whole_channels(channels, members, "pool-a", complete)
    with pytest.raises(HTTPException, match="409"):
        require_whole_channels(channels, members, "pool-a", complete[:1])
    with pytest.raises(HTTPException, match="409"):
        require_whole_channels(channels, members, "pool-a", (*complete, PoolBinding(deployment_id="b1", model="same")))


def test_channel_summary_has_name_pool_and_distinct_models() -> None:
    members: Final = (
        ChannelMember(channel_id="channel", deployment_id="one", model="same"),
        ChannelMember(channel_id="channel", deployment_id="two", model="same"),
        ChannelMember(channel_id="foreign", deployment_id="three", model="other"),
    )
    visible: Final = channel_view(_channel("channel", "pool"), members, "Source pool")
    assert visible["name"] == "channel"
    assert visible["pool_name"] == "Source pool"
    assert visible["models"] == ["same"]
    assert visible["deployment_count"] == 2
    assert visible["deployment_ids"] == ["one", "two"]
