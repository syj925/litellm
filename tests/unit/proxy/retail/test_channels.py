from typing import Final

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.channels import ChannelAddDeployments, ChannelCreate, ChannelEdit, prepare_channel_deployments
from litellm.types.router import Deployment


def _deployment(model: str, identifier: str = "incoming-id") -> Deployment:
    return Deployment(
        model_name=model,
        litellm_params={"model": "anthropic/" + model, "api_key": "local-only"},
        model_info={"id": identifier},
    )


def test_provisioning_assigns_new_ids_without_mutating_or_reusing_incoming_deployments() -> None:
    incoming: Final = (_deployment("first"), _deployment("second"))
    prepared: Final = prepare_channel_deployments(incoming)
    assert len({item.model_info.id for item in prepared}) == len(incoming)
    assert all(item.model_info.id != "incoming-id" for item in prepared)
    assert all(item.model_info.id == "incoming-id" for item in incoming)
    assert [item.model_name for item in prepared] == [item.model_name for item in incoming]
    assert prepared[0].litellm_params == incoming[0].litellm_params


def test_channel_creation_does_not_mix_existing_ids_with_new_models() -> None:
    with pytest.raises(ValidationError, match="existing deployments or create new"):
        ChannelCreate(name="Source A", pool_id="pool", deployment_ids=("existing",), deployments=(_deployment("a"),))
    with pytest.raises(ValidationError, match="distinct public model"):
        ChannelAddDeployments(deployments=(_deployment("same"), _deployment("same")))
    assert ChannelCreate(name=" Source A ", pool_id="pool", deployment_ids=("existing",)).name == "Source A"


def test_team_specific_alias_cannot_implicitly_join_a_gateway_resource_channel() -> None:
    model: Final = _deployment("a")
    team_model: Final = model.model_copy(update={"model_info": model.model_info.model_copy(update={"team_id": "team"})})
    with pytest.raises(ValidationError, match="team-specific alias"):
        ChannelAddDeployments(deployments=(team_model,))


def test_channel_edit_requires_the_version_the_operator_reviewed() -> None:
    with pytest.raises(ValidationError, match="expected_version"):
        ChannelEdit(name="Source A", pool_id="pool", deployment_ids=("one",))
    assert (
        ChannelEdit(name="Source A", pool_id="pool", deployment_ids=("one",), expected_version=2).expected_version == 2
    )
