import asyncio
from collections.abc import Iterator
from typing import Final

import pytest
from fastapi import HTTPException

from litellm.proxy.retail.pool_routing import (
    bind_pool_routing,
    clean_pool_metadata,
    filter_pool_deployments,
    guard_pool_deployment,
    mark_pool_failure,
    pool_cache_scope,
    pool_has_uncertain_attempt,
    pool_rejected_before_upstream,
    reset_pool_routing,
)
from litellm.proxy.retail.pool_store import PoolBinding, PoolRouteDecision
from litellm.proxy.retail.pricing import as_mapping


@pytest.fixture(autouse=True)
def fresh_request_context() -> Iterator[None]:
    reset_pool_routing()
    yield
    reset_pool_routing()


def route(pool: str = "pool-a") -> PoolRouteDecision:
    return PoolRouteDecision(
        pool_id=pool,
        pool_name="Example resource pool",
        settings_version=1,
        pool_version=1,
        allowed_deployment_ids=("allowed-a", "allowed-b"),
        bindings=(
            PoolBinding(deployment_id="allowed-a", model="public", weight=3, priority=0),
            PoolBinding(deployment_id="allowed-b", model="public", weight=1, priority=1),
        ),
    )


def test_pool_filters_physical_deployments_and_applies_weights_without_mutation() -> None:
    original: Final = {"model_info": {"id": "allowed-a"}, "litellm_params": {"model": "openai/example", "weight": 99}}
    bind_pool_routing("public", route(), {})
    filtered: Final = filter_pool_deployments((original, {"model_info": {"id": "foreign"}}))
    assert filtered == [
        {"model_info": {"id": "allowed-a"}, "litellm_params": {"model": "openai/example", "weight": 3, "order": 0}}
    ]
    assert original["litellm_params"]["weight"] == 99


def test_global_or_client_fallback_cannot_escape_selected_pool() -> None:
    bind_pool_routing("public", route(), {})
    with pytest.raises(HTTPException, match="Cross-pool"):
        filter_pool_deployments(({"model_info": {"id": "other-pool"}},))
    assert pool_rejected_before_upstream()


def test_specific_deployment_bypass_is_rechecked_before_transport() -> None:
    bind_pool_routing("public", route(), {})
    with pytest.raises(HTTPException, match="outside"):
        guard_pool_deployment({"model_info": {"id": "foreign"}, "metadata": {"model_info": {"id": "allowed-a"}}})
    assert pool_rejected_before_upstream()


def test_authorized_attempt_is_not_mistaken_for_a_pre_transport_rejection() -> None:
    bind_pool_routing("public", route(), {})
    guard_pool_deployment({"model_info": {"id": "allowed-a"}})
    with pytest.raises(HTTPException):
        filter_pool_deployments(({"model_info": {"id": "foreign"}},))
    assert not pool_rejected_before_upstream()


@pytest.mark.parametrize(
    "field", ["api_base", "api_key", "fallbacks", "litellm_params", "specific_deployment", "preset_cache_key"]
)
def test_request_routing_and_cache_overrides_cannot_bypass_key_policy(field: str) -> None:
    with pytest.raises(HTTPException, match="managed by your API key"):
        bind_pool_routing("public", route(), {field: "caller-value"})


def test_pooled_cache_is_scoped_by_customer_and_pool_and_retains_no_cache_control() -> None:
    first: Final = pool_cache_scope("customer-a", route(), {"cache": {"namespace": "forged", "no-cache": True}})
    other_pool: Final = pool_cache_scope("customer-a", route("pool-b"), {})
    other_customer: Final = pool_cache_scope("customer-b", route(), {})
    assert first == {"cache": {"namespace": "retail:customer-a:pool-a", "no-cache": True}}
    assert first != other_pool and first != other_customer
    assert pool_cache_scope("customer-a", None, {"cache": {"namespace": "old"}}) == {}


def test_caller_cannot_forge_pool_audit_metadata_but_unpooled_cache_metadata_is_preserved() -> None:
    metadata: Final = {
        "retail_resource_pool_id": "forged",
        "retail_request_id": "forged",
        "redis_namespace": "old",
        "session_id": "session",
    }
    assert clean_pool_metadata(metadata, route()) == {"session_id": "session"}
    assert clean_pool_metadata(metadata) == {"session_id": "session", "redis_namespace": "old"}


def test_uncertain_upstream_attempt_prevents_automatic_refund_after_a_blocked_fallback() -> None:
    bind_pool_routing("public", route(), {})
    guard_pool_deployment({"model_info": {"id": "allowed-a"}})
    mark_pool_failure(HTTPException(503, "Unknown upstream outcome"))
    mark_pool_failure(HTTPException(403, "Later fallback was blocked"))
    assert pool_has_uncertain_attempt()
    reset_pool_routing()
    assert not pool_has_uncertain_attempt() and not pool_rejected_before_upstream()


def test_definitive_upstream_rejection_does_not_create_an_uncertain_charge() -> None:
    bind_pool_routing("public", route(), {})
    guard_pool_deployment({"model_info": {"id": "allowed-a"}})
    mark_pool_failure(HTTPException(429, "No request accepted"))
    assert not pool_has_uncertain_attempt()


@pytest.mark.asyncio
async def test_concurrent_requests_keep_independent_resource_pool_decisions() -> None:
    first_ready: Final = asyncio.Event()
    second_ready: Final = asyncio.Event()

    async def resolve(pool_id: str, own_ready: asyncio.Event, other_ready: asyncio.Event) -> str:
        identity: Final = pool_id + "-deployment"
        decision: Final = PoolRouteDecision(
            pool_id=pool_id,
            pool_name=pool_id,
            settings_version=1,
            pool_version=1,
            allowed_deployment_ids=(identity,),
            bindings=(PoolBinding(deployment_id=identity, model="public", weight=1, priority=0),),
        )
        bind_pool_routing("public", decision, {})
        own_ready.set()
        await other_ready.wait()
        deployments: Final = filter_pool_deployments(
            (
                {"model_info": {"id": "first-deployment"}},
                {"model_info": {"id": "second-deployment"}},
            )
        )
        assert len(deployments) == 1
        return str(as_mapping(deployments[0]["model_info"])["id"])

    selected: Final = await asyncio.gather(
        resolve("first", first_ready, second_ready),
        resolve("second", second_ready, first_ready),
    )
    assert selected == ["first-deployment", "second-deployment"]


@pytest.mark.asyncio
async def test_retry_evidence_from_child_task_reaches_request_failure_handler() -> None:
    bind_pool_routing("public", route(), {})

    async def failed_attempt() -> None:
        guard_pool_deployment({"model_info": {"id": "allowed-a"}})
        mark_pool_failure(HTTPException(503, "Unknown result"))

    await asyncio.create_task(failed_attempt())
    assert pool_has_uncertain_attempt()
    with pytest.raises(HTTPException):
        filter_pool_deployments(({"model_info": {"id": "foreign"}},))
    assert not pool_rejected_before_upstream()


@pytest.mark.asyncio
async def test_child_task_rejection_with_no_transport_allows_safe_reservation_release() -> None:
    bind_pool_routing("public", route(), {})

    async def unavailable_route() -> None:
        with pytest.raises(HTTPException):
            filter_pool_deployments(({"model_info": {"id": "foreign"}},))

    await asyncio.create_task(unavailable_route())
    assert pool_rejected_before_upstream() and not pool_has_uncertain_attempt()
