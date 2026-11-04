from collections.abc import Mapping
from typing import Final

import pytest
from pydantic import SecretStr

from litellm.proxy.retail.custom_secret_adapter import (
    ManagedCredentialAdapter,
    ManagedSecretOverrideRejected,
    ManagedSecretUnavailable,
    is_managed_deployment,
    managed_credential_kwargs,
)
from litellm.proxy.retail.secret_cache import ManagedSecretCache
from litellm.proxy.retail.secret_config import ManagedSecretsConfig, SecretBinding
from litellm.proxy.retail.secret_provider import SecretResult, SecretValue

DEPLOYMENT: Final = {"litellm_params": {
    "model": "openai/fixture-model", "api_key": "retail-secret/main", "api_base": "https://upstream.test/v1",
}}


def test_reserved_prefix_is_closed_when_disabled_without_environment_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("main", "fixture-env-fallback-must-never-be-used")
    with pytest.raises(ManagedSecretUnavailable) as failure:
        managed_credential_kwargs(DEPLOYMENT, {}, adapter=ManagedCredentialAdapter())
    assert failure.value.status_code == 503
    assert failure.value.detail == "Managed credential is unavailable"


@pytest.mark.parametrize(
    "changes",
    (
        {"api_base": "https://foreign.test"}, {"base_url": "https://foreign.test"},
        {"api_key": "caller-fixture-key"}, {"client": object()}, {"custom_llm_provider": "other"},
        {"litellm_credential_name": "other"}, {"headers": {"Authorization": "fixture"}},
        {"default_headers": {"x-api-key": "fixture"}}, {"extra_headers": {"Authorization": "fixture"}},
    ),
)
def test_managed_credentials_reject_caller_routing_and_authentication_overrides(changes: Mapping[str, object]) -> None:
    with pytest.raises(ManagedSecretOverrideRejected) as failure:
        managed_credential_kwargs(DEPLOYMENT, changes, adapter=ManagedCredentialAdapter())
    assert failure.value.status_code == 400
    assert "foreign" not in str(failure.value.detail)


def test_unmanaged_models_pass_through_but_caller_cannot_select_a_managed_ref() -> None:
    deployment: Final = {"litellm_params": {"api_key": "ordinary-fixture-key"}}
    assert not is_managed_deployment(deployment)
    assert managed_credential_kwargs(deployment, {"api_key": "ordinary-caller-key"}) == {}
    with pytest.raises(ManagedSecretOverrideRejected):
        managed_credential_kwargs(deployment, {"api_key": "retail-secret/private"})


@pytest.mark.parametrize("model,provider", (("anthropic/fixture-model", None), ("fixture-model", None),
                                          ("openai/fixture-model", "anthropic")))
def test_unverified_provider_families_are_rejected_before_credential_read(model: str, provider: str | None) -> None:
    deployment: Final = {"litellm_params": {
        "model": model, "custom_llm_provider": provider,
        "api_key": "retail-secret/main", "api_base": "https://upstream.test/v1",
    }}
    with pytest.raises(ManagedSecretOverrideRejected):
        managed_credential_kwargs(deployment, {}, adapter=ManagedCredentialAdapter())


class Provider:
    async def read(self, binding: SecretBinding) -> SecretResult:
        return SecretValue(SecretStr("fixture-managed-key"), 1)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_final_server_copy_accepts_only_trusted_values_and_does_not_modify_request() -> None:
    settings: Final = ManagedSecretsConfig.model_validate({
        "endpoint": "https://vault.test", "refs": [{"ref": "main", "path": "keys/main"}],
    })
    subject: Final = ManagedSecretCache(settings, Provider(), clock=lambda: 0)
    adapter: Final = ManagedCredentialAdapter(subject)
    await subject.refresh()
    source: Final = {"api_key": "retail-secret/main", "api_base": "https://upstream.test/v1", "client": None}
    result: Final = managed_credential_kwargs(DEPLOYMENT, source, adapter=adapter, final=True)
    assert result == {"api_key": "fixture-managed-key", "api_base": "https://upstream.test/v1"}
    assert source == {"api_key": "retail-secret/main", "api_base": "https://upstream.test/v1", "client": None}
    assert adapter.read("main") == await adapter.async_read("main") == "fixture-managed-key"
    await subject.close()
