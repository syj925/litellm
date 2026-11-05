from collections.abc import Mapping
from typing import Final, TypeAlias
from uuid import UUID

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.request_policy_types import (
    ApplicablePolicy,
    CompiledRequestPolicy,
    Definition,
    RequestPolicyFailure,
    RouteKind,
    TargetKind,
    compile_policies,
)

PolicyValue: TypeAlias = tuple[RouteKind, ...] | int | float


def policy(kind: TargetKind, values: Mapping[str, PolicyValue] | None = None) -> ApplicablePolicy:
    return ApplicablePolicy(kind, UUID(int=1), 1, Definition.model_validate(values or {}))


@pytest.mark.parametrize("values", (
    {"temperature": True}, {"temperature": "0.5"}, {"temperature": float("nan")},
    {"temperature": 3}, {"top_p": float("inf")}, {"top_p": -1}, {"top_p": True},
    {"max_output_tokens": True}, {"max_output_tokens": 0}, {"max_output_tokens": "100"},
    {"seed": True}, {"seed": 9007199254740992}, {"seed": -9007199254740992},
    {"allowed_routes": ["raw"]}, {"extra_body": {}},
))
def test_definition_refuses_coercion_nonfinite_values_unsafe_integers_and_unknown_fields(values: object) -> None:
    with pytest.raises(ValidationError):
        Definition.model_validate(values)


def test_compile_intersects_routes_uses_smallest_cap_and_preserves_consistent_fixed_parameters() -> None:
    result: Final = compile_policies((
        policy("model", {"allowed_routes": ("chat.completions", "responses"), "max_output_tokens": 500, "temperature": 0.5}),
        policy("user", {"allowed_routes": ("responses", "messages"), "max_output_tokens": 200, "temperature": 0.5, "top_p": 0.7}),
        policy("key", {"max_output_tokens": 300, "seed": 17}),
    ))
    assert isinstance(result, CompiledRequestPolicy)
    assert (result.allowed_routes, result.max_output_tokens, result.temperature, result.top_p, result.seed) == (
        frozenset({"responses"}), 200, 0.5, 0.7, 17,
    )


@pytest.mark.parametrize("field,first,second", (("temperature", 0.1, 0.2), ("top_p", 0.3, 0.4), ("seed", 1, 2)))
def test_compile_conflicting_fixed_values_fail_closed(field: str, first: float, second: float) -> None:
    assert compile_policies((policy("user", {field: first}), policy("key", {field: second}))) == RequestPolicyFailure("conflict")


def test_compile_disjoint_routes_or_duplicate_targets_never_choose_one_rule() -> None:
    assert compile_policies((policy("user", {"allowed_routes": ("responses",)}), policy("key", {"allowed_routes": ("messages",)}))) == RequestPolicyFailure("conflict")
    assert compile_policies((policy("user"), policy("user"))) == RequestPolicyFailure("invalid")


def test_prompt_order_is_deterministic_private_and_bounded_in_utf8_bytes() -> None:
    one: Final = ApplicablePolicy("model", UUID(int=1), 1, Definition(), "private-model-instruction")
    two: Final = ApplicablePolicy("user", UUID(int=2), 2, Definition(), "private-user-instruction")
    result: Final = compile_policies((one, two))
    assert isinstance(result, CompiledRequestPolicy) and result.prompt is not None
    assert result.prompt.get_secret_value() == "private-model-instruction\n\nprivate-user-instruction"
    assert "private-model" not in repr(one) + repr(result)
    excessive: Final = ApplicablePolicy("key", UUID(int=3), 1, Definition(), "界" * 5462)
    assert compile_policies((excessive,)) == RequestPolicyFailure("capacity")


def test_four_native_scopes_intersect_and_order_prompts_without_per_scope_overrides() -> None:
    plans: Final = tuple(ApplicablePolicy(kind, UUID(int=index + 1), 1, Definition(
        max_output_tokens=cap, temperature=0.5, allowed_routes=routes,
    ), f"private-{kind}") for index, (kind, cap, routes) in enumerate((
        ("key", 400, ("chat.completions", "responses")), ("user", 300, ("responses", "messages")),
        ("team", 100, ("responses",)), ("model", 500, ("chat.completions", "responses", "messages")),
    )))
    result: Final = compile_policies(plans)
    assert isinstance(result, CompiledRequestPolicy) and result.prompt is not None
    assert result.max_output_tokens == 100 and result.allowed_routes == frozenset(("responses",))
    assert result.prompt.get_secret_value() == "private-model\n\nprivate-team\n\nprivate-user\n\nprivate-key"
    assert compile_policies((*plans, plans[2])) == RequestPolicyFailure("invalid")
    assert compile_policies((policy("team", {"temperature": 0.5}), policy("user", {"temperature": 1.0}))) == RequestPolicyFailure("conflict")


def test_four_scope_private_prompts_share_one_total_utf8_budget() -> None:
    plans: Final = tuple(ApplicablePolicy(kind, UUID(int=index + 1), 1, Definition(), "界" * 1400)
        for index, kind in enumerate(("model", "team", "user", "key")))
    assert compile_policies(plans) == RequestPolicyFailure("capacity")
