import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest
from pydantic import SecretStr, ValidationError

from litellm.proxy.retail.scim_config import ScimConfig, ScimSetup, load_scim_setup
from litellm.proxy.retail.scim_protocol import ScimFailure

_TOKEN: Final = "rt-scim." + "fixture-private-material_" * 3


def configuration(changes: Mapping[str, object] | None = None) -> ScimConfig:
    return ScimConfig.model_validate(
        {"namespace": "directory-test", "generation": 1, "public_origin": "https://gateway.test", **(changes or {})}
    )


def environment(path: Path, body: str, token: str = _TOKEN) -> Mapping[str, str]:
    path.write_text(body, encoding="utf-8")
    return {"RETAIL_SCIM_ENABLED": "true", "RETAIL_SCIM_CONFIG": str(path), "RETAIL_SCIM_TOKEN": token}


@pytest.mark.parametrize(
    "changes",
    (
        {"namespace": ""},
        {"namespace": "bad namespace"},
        {"namespace": "x" * 65},
        {"namespace": "目录"},
        {"generation": 0},
        {"generation": True},
        {"generation": "1"},
        {"generation": 9007199254740992},
        {"max_users": 0},
        {"max_users": 10001},
        {"max_users": True},
        {"max_users": "10"},
        {"allow_loopback_http": "true"},
        {"token": "fixture-private-token"},
    ),
)
def test_invalid_configuration_never_coerces_authority_or_accepts_secrets(changes: Mapping[str, object]) -> None:
    with pytest.raises(ValidationError) as failure:
        configuration(changes)
    assert "fixture-private-token" not in str(failure.value)


@pytest.mark.parametrize(
    "origin",
    (
        "http://gateway.test",
        "https://gateway.test/",
        "https://gateway.test/path",
        "https://gateway.test?query=1",
        "https://gateway.test#fragment",
        "https://user:fixture-private-password@gateway.test",
        "https://gateway.test:0",
        "https://gateway.test:bad",
        "https://gateway.test\\other",
        "https://gateway.test/%2f",
        "https://gateway.test\n",
    ),
)
def test_public_location_requires_exact_server_controlled_tls_origin(origin: str) -> None:
    with pytest.raises(ValidationError) as failure:
        configuration({"public_origin": origin})
    assert "fixture-private-password" not in str(failure.value)


@pytest.mark.parametrize("origin", ("http://localhost:1234", "http://127.0.0.1:1234", "http://[::1]:1234"))
def test_explicit_loopback_mode_accepts_only_local_test_origins(origin: str) -> None:
    assert configuration({"public_origin": origin, "allow_loopback_http": True}).public_origin == origin
    with pytest.raises(ValidationError):
        configuration({"public_origin": "https://gateway.test", "allow_loopback_http": True})


def test_setup_digest_fences_full_token_and_every_configuration_change_without_exposing_them() -> None:
    first: Final = ScimSetup(configuration(), SecretStr(_TOKEN))
    same: Final = ScimSetup(configuration(), SecretStr(_TOKEN))
    assert first.digest() == same.digest() and len(first.digest()) == 64
    assert "fixture-private" not in repr(first) and "gateway.test" not in repr(first)
    for changes in (
        {"generation": 2},
        {"namespace": "other"},
        {"max_users": 999},
        {"public_origin": "https://other.test"},
    ):
        assert ScimSetup(configuration(changes), SecretStr(_TOKEN)).digest() != first.digest()
    assert ScimSetup(configuration(), SecretStr(_TOKEN + "x")).digest() != first.digest()


def test_disabled_feature_returns_none_without_reading_configuration_or_requiring_a_token() -> None:
    assert load_scim_setup({}) is None
    assert load_scim_setup({"RETAIL_SCIM_ENABLED": "false", "RETAIL_SCIM_CONFIG": "missing-file"}) is None


@pytest.mark.parametrize(
    "token",
    (
        "",
        "sk-ordinary",
        "rt-scim." + "x" * 31,
        "rt-scim." + "x" * 257,
        "rt-scim." + "x" * 32 + "=",
        "rt-scim." + "x" * 32 + "\n",
        "RT-SCIM." + "x" * 32,
    ),
)
def test_enabled_receiver_requires_a_distinct_bounded_opaque_bearer(tmp_path: Path, token: str) -> None:
    result: Final = load_scim_setup(environment(tmp_path / "identity.json", configuration().model_dump_json(), token))
    assert result == ScimFailure(503)
    assert token not in repr(result) or token == ""


def test_loader_reads_only_server_file_and_preserves_valid_defaults(tmp_path: Path) -> None:
    result: Final = load_scim_setup(environment(tmp_path / "identity.json", configuration().model_dump_json()))
    assert isinstance(result, ScimSetup)
    assert result.config.max_users == 1000 and result.token.get_secret_value() == _TOKEN
    assert "fixture-private" not in repr(result)


@pytest.mark.parametrize(
    "body",
    ("not-json", "{}", "[1]", '{"namespace":"a","generation":1,"generation":2,"public_origin":"https://gateway.test"}'),
)
def test_malformed_or_duplicate_configuration_fails_without_echo(tmp_path: Path, body: str) -> None:
    assert load_scim_setup(environment(tmp_path / "identity.json", body)) == ScimFailure(503)


def test_config_file_bound_is_checked_before_parsing_and_includes_whitespace(tmp_path: Path) -> None:
    body: Final = configuration().model_dump_json()
    padded: Final = body + " " * (131072 - len(body))
    assert isinstance(load_scim_setup(environment(tmp_path / "identity.json", padded)), ScimSetup)
    assert load_scim_setup(environment(tmp_path / "identity.json", padded + " ")) == ScimFailure(503)
    assert load_scim_setup({"RETAIL_SCIM_ENABLED": "true", "RETAIL_SCIM_TOKEN": _TOKEN}) == ScimFailure(503)
    assert json.loads(body)["namespace"] == configuration().namespace


def test_deeply_nested_configuration_is_an_optional_failure_instead_of_a_startup_exception(tmp_path: Path) -> None:
    assert load_scim_setup(environment(tmp_path / "identity.json", "[" * 1100 + "]" * 1100)) == ScimFailure(503)
