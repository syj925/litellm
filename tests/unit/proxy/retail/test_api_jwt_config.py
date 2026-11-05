import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.api_jwt_config import ApiJwtConfig, ApiJwtFailure, load_api_jwt_config


def source() -> Mapping[str, object]:
    return {
        "namespace": "fixture", "generation": 1, "issuer": "https://issuer.test", "audience": "gateway",
        "client_id": "client", "jwks_uri": "https://keys.test/jwks",
        "bindings": ({"subject": "subject", "user_id": "owner", "token_hash": "a" * 64,
                      "binding_revision": str(uuid4())},),
    }


def test_private_config_frozen_digest_tracks_all_authority_fields() -> None:
    settings: Final = ApiJwtConfig.model_validate(source())
    assert "subject" not in repr(settings) and "keys.test" not in repr(settings)
    assert settings.digest() == ApiJwtConfig.model_validate_json(settings.model_dump_json()).digest()
    altered: Final = settings.model_copy(update={"generation": 2})
    assert altered.digest() != settings.digest()
    with pytest.raises(ValidationError):
        settings.generation = 2


@pytest.mark.parametrize("field,value", (
    ("generation", True), ("generation", 0), ("generation", 9007199254740992),
    ("namespace", "bad namespace"), ("max_token_seconds", 59), ("max_token_seconds", 901),
    ("max_token_seconds", True), ("required_scope", "models:invoke other"),
    ("issuer", "http://issuer.test"), ("jwks_uri", "https://keys.test/jwks?redirect=elsewhere"),
    ("jwks_uri", "https://user:password@keys.test/jwks"), ("audience", "line\nbreak"),
))
def test_invalid_configuration_cannot_load(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ApiJwtConfig.model_validate({**source(), field: value})


def test_only_all_loopback_endpoints_may_opt_into_http() -> None:
    settings: Final = ApiJwtConfig.model_validate({
        **source(), "issuer": "http://127.0.0.1:18000", "jwks_uri": "http://localhost:18001/jwks",
        "allow_loopback_http": True,
    })
    assert settings.allow_loopback_http
    with pytest.raises(ValidationError):
        ApiJwtConfig.model_validate({**settings.model_dump(), "jwks_uri": "https://keys.test/jwks"})


def test_duplicate_subject_or_revision_and_unbounded_bindings_fail() -> None:
    values: Final = source()
    settings: Final = ApiJwtConfig.model_validate(values)
    first: Final = settings.bindings[0].model_dump()
    for rows in (
        (first, {**first, "binding_revision": uuid4()}),
        (first, {**first, "subject": "other"}),
        tuple({**first, "subject": str(number), "binding_revision": uuid4()} for number in range(129)),
    ):
        with pytest.raises(ValidationError):
            ApiJwtConfig.model_validate({**values, "bindings": rows})


def test_loader_is_opt_in_bounded_and_never_echoes_bad_inputs(tmp_path: Path) -> None:
    config: Final = tmp_path / "identity.json"
    config.write_text(json.dumps(source()), encoding="utf-8")
    environment: Final = {"RETAIL_API_JWT_ENABLED": "true", "RETAIL_API_JWT_CONFIG": str(config)}
    assert load_api_jwt_config({}) is None
    assert isinstance(load_api_jwt_config(environment), ApiJwtConfig)
    config.write_text("private-value" * 12000, encoding="utf-8")
    assert load_api_jwt_config(environment) == ApiJwtFailure("invalid")
    assert load_api_jwt_config({"RETAIL_API_JWT_ENABLED": "true"}) == ApiJwtFailure("invalid")
