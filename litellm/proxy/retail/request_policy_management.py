from dataclasses import dataclass, field
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from .request_policy_types import Definition

ResourceKind: TypeAlias = Literal["prompts", "policies", "bindings"]
TargetKind: TypeAlias = Literal["user", "key", "model", "team"]


@dataclass(frozen=True, slots=True)
class PolicyActor:
    user_id: str | None = field(repr=False)
    token_hash: str | None = field(repr=False)
    trusted_master: bool = False
    request_route: str = "/retail/admin/request-policies/policies"


class NamedInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=100)
    enabled: StrictBool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if value != value.strip() or any(ord(character)<32 for character in value):
            raise ValueError("A bounded name is required")
        return value


class PromptCreate(NamedInput):
    content: str = Field(max_length=8000, repr=False)

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        if len(value.encode())>8000 or "\x00" in value:
            raise ValueError("Prompt text exceeds the supported bound")
        return value


class PromptUpdate(PromptCreate):
    expected_version: StrictInt = Field(ge=1, le=9007199254740991)


class PolicyCreate(NamedInput):
    definition: Definition
    prompt_id: UUID | None = None


class PolicyUpdate(PolicyCreate):
    expected_version: StrictInt = Field(ge=1, le=9007199254740991)


class BindingCreate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    target_id: UUID
    policy_id: UUID
    enabled: StrictBool = True


class BindingUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    policy_id: UUID
    enabled: StrictBool
    expected_version: StrictInt = Field(ge=1, le=9007199254740991)


class PolicyTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    id: UUID
    kind: TargetKind
    label: str


class ResourceRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    id: UUID
    enabled: bool
    version: int
    created_at: AwareDatetime
    updated_at: AwareDatetime


class PromptRow(ResourceRow):
    name: str
    content: str = Field(repr=False)


class PolicyRow(ResourceRow):
    name: str
    definition: Definition
    prompt_id: UUID | None


class BindingRow(ResourceRow):
    target_id: UUID
    policy_id: UUID
    target_kind: TargetKind
    target_label: str
    policy_name: str
