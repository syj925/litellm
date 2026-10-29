from datetime import datetime, timezone
from typing import Final

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from litellm.proxy.retail.pool_store import (
    PoolAccess,
    PoolBinding,
    PoolDefinition,
    PoolGrant,
    ResourcePolicy,
    _PoolRow,
    _Settings,
    _StoredBinding,
    allowed_model_names,
    choose_route,
    deployment_catalog,
    effective_account_grants,
    effective_key_grants,
    validate_bindings,
)


def _pool(identifier: str, *, priority: int = 100, enabled: bool = True, visibility: str = "public") -> _PoolRow:
    return _PoolRow.model_validate(
        {
            "id": identifier,
            "name": identifier,
            "priority": priority,
            "enabled": enabled,
            "visibility": visibility,
            "version": 1,
            "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    )


def _binding(pool: str, deployment: str, model: str) -> _StoredBinding:
    return _StoredBinding(pool_id=pool, deployment_id=deployment, model=model)


def _deployment(deployment: str, model: str) -> dict[str, object]:
    return {"model_name": model, "model_info": {"id": deployment}, "litellm_params": {"model": "provider/example"}}


def test_new_pools_are_disabled_and_restricted_until_explicitly_published_and_granted() -> None:
    pool: Final = PoolDefinition(id="new", name="New")
    assert pool.enabled is False
    assert pool.visibility == "restricted"
    assert pool.default_discount == "1"


def test_deployment_catalog_never_exposes_keys_or_upstream_hosts() -> None:
    raw: Final = _deployment("deployment-a", "public-a")
    record: Final = {
        **raw,
        "litellm_params": {
            "model": "anthropic/private-resource-name",
            "api_key": "test-secret",
            "api_base": "https://private.invalid",
            "litellm_credential_name": "stored-credential",
        },
    }
    visible: Final = deployment_catalog((record,))[0].model_dump()
    assert visible == {
        "deployment_id": "deployment-a",
        "model": "public-a",
        "provider": "anthropic",
        "credential_name": "stored-credential",
    }
    assert "test-secret" not in str(visible)
    assert "private.invalid" not in str(visible)


@pytest.mark.parametrize(
    "bindings",
    (
        (PoolBinding(deployment_id="deployment-a", model="wrong-public-model"),),
        (PoolBinding(deployment_id="not-a-deployment", model="public-a"),),
        (
            PoolBinding(deployment_id="deployment-a", model="public-a"),
            PoolBinding(deployment_id="deployment-a", model="public-a"),
        ),
    ),
)
def test_bindings_require_unique_exact_deployment_and_public_model_pairs(bindings: tuple[PoolBinding, ...]) -> None:
    with pytest.raises(HTTPException) as failure:
        validate_bindings(bindings, deployment_catalog((_deployment("deployment-a", "public-a"),)))
    assert failure.value.status_code == 400


def test_account_grants_do_not_form_a_cartesian_product_between_pools_and_models() -> None:
    pools: Final = (_pool("pool-a"), _pool("pool-b"))
    bindings: Final = (
        _binding("pool-a", "a-a", "model-a"),
        _binding("pool-a", "a-b", "model-b"),
        _binding("pool-b", "b-a", "model-a"),
        _binding("pool-b", "b-b", "model-b"),
    )
    access: Final = PoolAccess(
        mode="custom",
        grants=(
            PoolGrant(pool_id="pool-a", models=("model-a",)),
            PoolGrant(pool_id="pool-b", models=("model-b",)),
        ),
    )
    account: Final = effective_account_grants(access, pools, bindings)
    forbidden: Final = ResourcePolicy(mode="custom", grants=(PoolGrant(pool_id="pool-a", models=("model-b",)),))
    with pytest.raises(HTTPException) as failure:
        effective_key_grants(forbidden, account, strict=True)
    assert failure.value.status_code == 403
    assert effective_key_grants(forbidden, account, strict=False) == ()
    narrowed: Final = effective_key_grants(
        ResourcePolicy(mode="custom", grants=(PoolGrant(pool_id="pool-a"),)), account, strict=True
    )
    assert narrowed == (PoolGrant(pool_id="pool-a", models=("model-a",)),)


def test_inherited_accounts_receive_only_enabled_public_pools() -> None:
    pools: Final = (_pool("public"), _pool("private", visibility="restricted"), _pool("disabled", enabled=False))
    bindings: Final = tuple(_binding(pool.id, pool.id, "model-a") for pool in pools)
    assert effective_account_grants(PoolAccess(), pools, bindings) == (
        PoolGrant(pool_id="public", models=("model-a",)),
    )


def test_only_unconfigured_legacy_keys_bypass_routing_while_global_routing_is_disabled() -> None:
    settings: Final = _Settings(routing_enabled=False, version=1)
    assert choose_route(settings, (), (), PoolAccess(), "model-a", None, ()) is None
    with pytest.raises(HTTPException) as failure:
        choose_route(settings, (), (), PoolAccess(), "model-a", {"mode": "auto"}, ())
    assert failure.value.status_code == 403


def test_auto_uses_site_priority_and_fixed_policy_stays_with_the_selected_allowed_pool() -> None:
    pools: Final = (_pool("first", priority=1), _pool("second", priority=20))
    bindings: Final = (_binding("first", "a", "model-a"), _binding("second", "b", "model-a"))
    deployments: Final = (_deployment("a", "model-a"), _deployment("b", "model-a"))
    settings: Final = _Settings(routing_enabled=True, version=4)
    automatic: Final = choose_route(settings, pools, bindings, PoolAccess(), "model-a", None, deployments)
    fixed: Final = choose_route(
        settings, pools, bindings, PoolAccess(), "model-a", {"mode": "fixed", "pool_id": "second"}, deployments
    )
    assert automatic is not None and automatic.pool_id == "first" and automatic.allowed_deployment_ids == ("a",)
    assert fixed is not None and fixed.pool_id == "second" and fixed.allowed_deployment_ids == ("b",)
    assert fixed.settings_version == 4


def test_revocations_disable_new_requests_without_mutating_an_already_frozen_decision() -> None:
    pool: Final = _pool("only")
    bindings: Final = (_binding("only", "a", "model-a"),)
    deployments: Final = (_deployment("a", "model-a"),)
    settings: Final = _Settings(routing_enabled=True, version=1)
    saved: Final = choose_route(settings, (pool,), bindings, PoolAccess(), "model-a", None, deployments)
    for access, pools in (
        (PoolAccess(mode="custom"), (pool,)),
        (PoolAccess(), (pool.model_copy(update={"enabled": False}),)),
    ):
        with pytest.raises(HTTPException) as failure:
            choose_route(settings, pools, bindings, access, "model-a", None, deployments)
        assert failure.value.status_code == 403
    assert saved is not None and saved.allowed_deployment_ids == ("a",)
    with pytest.raises(ValidationError):
        saved.pool_id = "other"


def test_removed_native_deployments_are_unavailable_without_broadening_to_other_pool_resources() -> None:
    with pytest.raises(HTTPException) as failure:
        choose_route(
            _Settings(routing_enabled=True, version=1),
            (_pool("only"),),
            (_binding("only", "removed", "model-a"),),
            PoolAccess(),
            "model-a",
            None,
            (_deployment("unassigned", "model-a"),),
        )
    assert failure.value.status_code == 503


def test_model_discovery_filters_to_the_fixed_pool_and_excludes_models_available_only_elsewhere() -> None:
    pools: Final = (_pool("pool-a"), _pool("pool-b"))
    bindings: Final = (
        _binding("pool-a", "a-shared", "shared"),
        _binding("pool-a", "a-retry", "retry"),
        _binding("pool-b", "b-shared", "shared"),
        _binding("pool-b", "b-other", "other"),
        _binding("pool-b", "b-anthropic", "anthropic"),
    )
    deployments: Final = tuple(_deployment(binding.deployment_id, binding.model) for binding in bindings)
    names: Final = allowed_model_names(
        PoolAccess(),
        pools,
        bindings,
        {"mode": "fixed", "pool_id": "pool-b"},
        deployments,
    )
    assert names == frozenset({"shared", "other", "anthropic"})
    assert "retry" not in names


def test_model_discovery_revalidates_paired_custom_key_grants_without_a_cross_pool_product() -> None:
    pools: Final = (_pool("pool-a"), _pool("pool-b"))
    bindings: Final = (
        _binding("pool-a", "a-shared", "shared"),
        _binding("pool-a", "a-other", "other"),
        _binding("pool-b", "b-shared", "shared"),
        _binding("pool-b", "b-other", "other"),
    )
    deployments: Final = tuple(_deployment(binding.deployment_id, binding.model) for binding in bindings)
    access: Final = PoolAccess(
        mode="custom",
        grants=(
            PoolGrant(pool_id="pool-a", models=("shared",)),
            PoolGrant(pool_id="pool-b", models=("other",)),
        ),
    )
    denied: Final = {
        "mode": "custom",
        "grants": [
            {"pool_id": "pool-a", "models": ["other"]},
            {"pool_id": "pool-b", "models": ["shared"]},
        ],
    }
    assert allowed_model_names(access, pools, bindings, denied, deployments) == frozenset()
    assert allowed_model_names(access, pools, bindings, {"mode": "auto"}, deployments) == frozenset({"shared", "other"})
    assert allowed_model_names(PoolAccess(mode="custom"), pools, bindings, {"mode": "auto"}, deployments) == frozenset()


@pytest.mark.parametrize("policy", ({"mode": "unknown"}, {"mode": "fixed"}, {"mode": "auto", "pool_id": "only"}))
def test_invalid_stored_key_policies_fail_closed_even_when_global_routing_is_disabled(policy: dict[str, str]) -> None:
    with pytest.raises(HTTPException) as failure:
        choose_route(
            _Settings(routing_enabled=False, version=1),
            (_pool("only"),),
            (_binding("only", "a", "model-a"),),
            PoolAccess(),
            "model-a",
            policy,
            (_deployment("a", "model-a"),),
        )
    assert failure.value.status_code == 403
