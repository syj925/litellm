import json
import os
from collections.abc import Mapping, Sequence
from typing import Final

from fastapi import HTTPException
from pydantic import BaseModel, JsonValue, TypeAdapter

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

from .key_policy import POLICY_KEY

_JSON_METADATA: Final = TypeAdapter(dict[str, JsonValue])
_CUSTOMER_ROLES: Final = frozenset(
    {LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.INTERNAL_USER_VIEW_ONLY, LitellmUserRoles.CUSTOMER}
)
_PERSONAL_KEY_FIELDS: Final = frozenset(
    {
        "key_alias", "duration", "models", "max_budget", "budget_duration", "max_parallel_requests",
        "tpm_limit", "rpm_limit", "user_id", "team_id", "metadata", "key_type",
    }
)
_MODEL_INFO_FIELDS: Final = frozenset(
    {
        "mode", "max_tokens", "max_input_tokens", "max_output_tokens", "supports_system_messages",
        "supports_function_calling", "supports_parallel_function_calling", "supports_vision", "supports_audio_input",
        "supports_audio_output", "supports_prompt_caching", "supports_response_schema", "supports_reasoning",
        "supports_web_search", "supported_openai_params", "supports_tool_choice", "supports_pdf_input",
    }
)
_KEY_INFO_FIELDS: Final = frozenset(
    {
        "key", "token", "token_id", "key_name", "key_alias", "key_type", "user_id", "team_id", "organization_id",
        "models", "max_budget", "budget_duration", "budget_reset_at", "budget_limits", "tpm_limit", "rpm_limit",
        "max_parallel_requests", "expires", "blocked", "status", "created_at", "updated_at", "last_active",
        "created_by", "updated_by", "deleted_at", "deleted_by", "auto_rotate", "rotation_interval", "last_rotation_at",
    }
)


def is_retail_customer(auth: UserAPIKeyAuth) -> bool:
    return os.getenv("RETAIL_PORTAL_ENABLED", "false").lower() == "true" and auth.user_role in _CUSTOMER_ROLES


def needs_customer_privacy(auth: UserAPIKeyAuth) -> bool:
    return os.getenv("RETAIL_PORTAL_ENABLED", "false").lower() == "true" and auth.user_role not in {
        LitellmUserRoles.PROXY_ADMIN, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY,
    }


def is_customer_personal_key(auth: UserAPIKeyAuth, team_id: str | None) -> bool:
    return is_retail_customer(auth) and team_id in {None, UI_SESSION_TOKEN_TEAM_ID}


def customer_resource_policy(auth: UserAPIKeyAuth) -> object | None:
    metadata: Final = auth.metadata or {}
    if POLICY_KEY in metadata:
        policy: Final = metadata[POLICY_KEY]
        if policy is None:
            raise HTTPException(403, "This API key contains an invalid resource policy")
        return policy
    return {"mode": "auto"} if is_customer_personal_key(auth, auth.team_id) else None


async def customer_model_deployment_ids(auth: UserAPIKeyAuth) -> frozenset[str] | None:
    if not is_retail_customer(auth):
        return None
    from .pool_store import PoolStore, router_deployments

    return await PoolStore().key_deployment_ids(
        auth.user_id or "", customer_resource_policy(auth), router_deployments(),
    )


def validate_customer_key_request(
    data: BaseModel, auth: UserAPIKeyAuth, *, existing_team_id: str | None = None,
    existing_user_id: str | None = None, updating: bool = False,
) -> None:
    if not is_retail_customer(auth):
        return
    requested: Final = data.model_dump(exclude_unset=True)
    requested_team: Final = requested.get("team_id", existing_team_id)
    if requested_team == UI_SESSION_TOKEN_TEAM_ID or existing_team_id == UI_SESSION_TOKEN_TEAM_ID:
        raise HTTPException(403, "Dashboard sessions cannot be created or modified through API key management")
    if auth.user_role == LitellmUserRoles.INTERNAL_USER_VIEW_ONLY:
        raise HTTPException(403, "This account has read-only access")
    if requested_team is not None or existing_team_id is not None:
        return
    permitted: Final = _PERSONAL_KEY_FIELDS | ({"key", "grace_period"} if updating else set())
    forbidden: Final = frozenset(requested) - permitted
    if forbidden:
        raise HTTPException(403, f"Customer API keys cannot configure: {', '.join(sorted(forbidden))}")
    if requested.get("key_type", "llm_api") != "llm_api":
        raise HTTPException(403, "Customer accounts can create AI API keys only")
    owner: Final = requested.get("user_id", existing_user_id)
    if owner is not None and owner != auth.user_id:
        raise HTTPException(403, "Customer API keys must belong to the signed-in account")
    metadata: Final = requested.get("metadata")
    if metadata is not None and (not isinstance(metadata, Mapping) or set(metadata) - {POLICY_KEY}):
        raise HTTPException(403, "Customer API keys can configure resource routing metadata only")
    policy: Final = metadata.get(POLICY_KEY) if isinstance(metadata, Mapping) else None
    if isinstance(policy, Mapping) and policy.get("mode") == "legacy":
        raise HTTPException(403, "Customer API keys must use account-authorized resource pools")


def customer_key_metadata(metadata: object, existing_metadata: object = None) -> dict[str, JsonValue]:
    existing: Final = (
        _JSON_METADATA.validate_python(existing_metadata) if isinstance(existing_metadata, Mapping) else {}
    )
    incoming: Final = _JSON_METADATA.validate_python(metadata) if isinstance(metadata, Mapping) else {}
    merged: Final = {**existing, **incoming}
    return merged if POLICY_KEY in merged else {**merged, POLICY_KEY: {"mode": "auto"}}


def customer_key_generate_payload(
    payload: Mapping[str, object], auth: UserAPIKeyAuth, team_id: str | None,
) -> dict[str, object]:
    if not is_customer_personal_key(auth, team_id):
        return dict(payload)
    return {
        **payload, "key_type": "llm_api", "allowed_routes": ["llm_api_routes"],
        "metadata": customer_key_metadata(payload.get("metadata")),
    }


def safe_customer_key_info(row: Mapping[str, object]) -> dict[str, object]:
    metadata: Final = row.get("metadata")
    policy: Final = metadata.get(POLICY_KEY, {"mode": "auto"}) if isinstance(metadata, Mapping) else {"mode": "auto"}
    profiles: Final = {key: _safe_key_user(row[key]) for key in ("user", "created_by_user") if key in row}
    windows: Final = {"budget_limits": _safe_key_budget_windows(row["budget_limits"])} if "budget_limits" in row else {}
    return {
        **{key: value for key, value in row.items() if key in _KEY_INFO_FIELDS},
        **profiles, **windows, "metadata": {POLICY_KEY: policy},
    }


def _safe_key_budget_windows(windows: object) -> list[dict[str, object]] | None:
    try:
        values: Final = json.loads(windows) if isinstance(windows, str) else windows
    except ValueError:
        return None
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None
    return [_safe_key_budget_window(value) for value in values if isinstance(value, Mapping)]


def _safe_key_budget_window(window: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value for key, value in window.items()
        if key in {"budget_limit", "max_budget", "time_period", "budget_duration", "reset_at"}
    }


def _safe_key_user(user: object) -> dict[str, object]:
    if not isinstance(user, Mapping):
        return {}
    return {key: value for key, value in user.items() if key in {"user_id", "user_email", "user_alias"}}


def safe_customer_model_info(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "model_name": row.get("model_name"),
            "litellm_params": {"model": row.get("model_name")},
            "model_info": _safe_model_capabilities(row.get("model_info")),
        }
        for row in rows
    ]


def safe_customer_model_group(row: Mapping[str, object]) -> dict[str, object]:
    return {"model_group": row.get("model_group"), **_safe_model_capabilities(row)}


def _safe_model_capabilities(info: object) -> dict[str, object]:
    if not isinstance(info, Mapping):
        return {}
    return {key: value for key, value in info.items() if key in _MODEL_INFO_FIELDS}
