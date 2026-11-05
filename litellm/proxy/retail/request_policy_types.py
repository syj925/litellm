from dataclasses import dataclass, field
from typing import Final, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt

from litellm.proxy._types import (
    LiteLLM_BudgetTable,
    LiteLLM_TeamMembership,
    LiteLLM_TeamTable,
    LiteLLM_UserTable,
    UserAPIKeyAuth,
)

RouteKind: TypeAlias = Literal["chat.completions", "responses", "messages"]
TargetKind: TypeAlias = Literal["model", "team", "user", "key"]


class Definition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    allowed_routes: tuple[RouteKind, ...] | None = Field(default=None, max_length=3)
    max_output_tokens: StrictInt | None = Field(default=None, ge=1, le=1_000_000)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False, strict=True)
    top_p: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)
    seed: StrictInt | None = Field(default=None, ge=-9007199254740991, le=9007199254740991)


@dataclass(frozen=True, slots=True)
class RequestPolicyFailure:
    code: Literal["forbidden", "conflict", "unavailable", "not_found", "invalid", "capacity"]


@dataclass(frozen=True, slots=True)
class ApplicablePolicy:
    target_kind: TargetKind
    policy_id: UUID
    policy_version: int
    definition: Definition = field(repr=False)
    prompt_text: str | None = field(default=None, repr=False)
    prompt_id: UUID | None = field(default=None, repr=False)
    prompt_version: int | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedRequestPolicies:
    auth: UserAPIKeyAuth = field(repr=False)
    user: LiteLLM_UserTable | None = field(repr=False)
    target_hash: str = field(repr=False)
    policies: tuple[ApplicablePolicy, ...] = field(repr=False)
    team: LiteLLM_TeamTable | None = field(default=None, repr=False)
    team_membership: LiteLLM_TeamMembership | None = field(default=None, repr=False)
    team_member_default_budget: LiteLLM_BudgetTable | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CompiledRequestPolicy:
    allowed_routes: frozenset[RouteKind] | None
    max_output_tokens: int | None
    temperature: float | None
    top_p: float | None
    seed: int | None
    prompt: SecretStr | None = field(repr=False)


def compile_policies(policies: tuple[ApplicablePolicy, ...]) -> CompiledRequestPolicy | RequestPolicyFailure:
    if len(policies) > 4 or len(frozenset(policy.target_kind for policy in policies)) != len(policies):
        return RequestPolicyFailure("invalid")
    route_sets: Final = tuple(frozenset(policy.definition.allowed_routes) for policy in policies
        if policy.definition.allowed_routes is not None)
    allowed: Final = frozenset.intersection(*route_sets) if route_sets else None
    if allowed == frozenset():
        return RequestPolicyFailure("conflict")
    temperatures: Final = frozenset(policy.definition.temperature for policy in policies
        if policy.definition.temperature is not None)
    probabilities: Final = frozenset(policy.definition.top_p for policy in policies
        if policy.definition.top_p is not None)
    seeds: Final = frozenset(policy.definition.seed for policy in policies if policy.definition.seed is not None)
    if any(len(values) > 1 for values in (temperatures, probabilities, seeds)):
        return RequestPolicyFailure("conflict")
    caps: Final = tuple(policy.definition.max_output_tokens for policy in policies
        if policy.definition.max_output_tokens is not None)
    ordered: Final = sorted(policies, key=lambda policy: ("model", "team", "user", "key").index(policy.target_kind))
    prompts: Final = tuple(policy.prompt_text for policy in ordered if policy.prompt_text)
    prompt: Final = "\n\n".join(prompts)
    if len(prompt.encode("utf-8")) > 16384:
        return RequestPolicyFailure("capacity")
    return CompiledRequestPolicy(
        allowed, min(caps) if caps else None, next(iter(temperatures), None),
        next(iter(probabilities), None), next(iter(seeds), None), SecretStr(prompt) if prompt else None,
    )
