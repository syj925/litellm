from collections.abc import Mapping
from typing import Final

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.secret_config import ManagedSecretsConfig, load_secret_config


def configuration(changes: Mapping[str, object] | None = None) -> ManagedSecretsConfig:
    return ManagedSecretsConfig.model_validate(
        {"endpoint": "https://vault.test", "refs": [{"ref": "main", "path": "models/main"}], **(changes or {})}
    )


@pytest.mark.parametrize(
    "change",
    (
        {"endpoint": "http://vault.test"},
        {"endpoint": "https://user:password@vault.test"},
        {"endpoint": "https://vault.test/path"},
        {"endpoint": "https://vault.test?token=fixture"},
        {"endpoint": "https://vault.test#fragment"},
        {"endpoint": "https://vault.test:99999"},
        {"endpoint": "file:///tmp/secrets"},
        {"mount": "../secret"},
        {"namespace": "a\r\nb"},
        {"token_env": "unsafe-name"},
        {"refs": [{"ref": "a", "path": "../private"}]},
        {"refs": [{"ref": "a", "path": "models/%2e%2e/key"}]},
        {"refs": [{"ref": "a", "path": "one"}, {"ref": "a", "path": "two"}]},
        {"refs": []},
        {"ttl_seconds": 0},
        {"ttl_seconds": True},
        {"refresh_seconds": 31},
        {"timeout_seconds": 11},
        {"retries": 3},
        {"concurrency": 9},
        {"max_response_bytes": 1048577},
        {"unrecognized": "private-fixture"},
    ),
)
def test_provider_configuration_rejects_unbounded_or_untrusted_locations(change: Mapping[str, object]) -> None:
    with pytest.raises(ValidationError):
        configuration(change)


def test_http_requires_explicit_loopback_fixture_configuration() -> None:
    value: Final = configuration({"endpoint": "http://127.0.0.1:12345", "allow_loopback_http": True})
    assert value.endpoint == "http://127.0.0.1:12345"
    with pytest.raises(ValidationError):
        configuration({"endpoint": "http://external.test", "allow_loopback_http": True})


def test_disabled_managed_secrets_does_not_require_or_read_configuration() -> None:
    assert load_secret_config({"RETAIL_MANAGED_SECRETS_CONFIG": "/fixture/does-not-exist"}) is None


def test_configuration_repr_hides_endpoint_and_secret_paths() -> None:
    value: Final = configuration({"endpoint": "https://private-fixture.test"})
    assert "private-fixture" not in repr(value)
    assert "models/main" not in repr(value.refs[0])
