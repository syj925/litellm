import json
from collections.abc import Mapping, Sequence
from typing import Final, TypeVar

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from litellm.proxy._types import UserAPIKeyAuth

from .customer_access import is_retail_customer, safe_customer_key_info

_Payload = TypeVar("_Payload")
_LOG_FIELDS: Final = frozenset(
    {
        "request_id", "litellm_call_id", "session_id", "call_type", "api_key", "total_tokens", "prompt_tokens",
        "completion_tokens", "startTime", "endTime", "completionStartTime", "user", "team_id", "organization_id",
        "end_user", "status", "cache_hit", "requester_ip_address", "request_duration_ms", "ttft_ms",
        "session_total_count", "session_total_duration_ms", "session_cache_hit_count", "session_llm_count",
        "session_agent_count", "session_total_prompt_tokens", "session_total_completion_tokens", "session_total_tokens",
        "mcp_tool_call_count", "date",
    }
)
_LOG_METADATA_FIELDS: Final = frozenset(
    {
        "status", "user_api_key_alias", "user_api_key_team_alias", "user_api_key_user_id", "user_api_key_team_id",
        "user_api_key_end_user_id", "retail_request_id", "retail_resource_pool_id", "retail_resource_pool_name",
        "retail_resource_source_kind",
    }
)
_PAGE_FIELDS: Final = frozenset(
    {"total", "page", "page_size", "total_pages", "total_is_capped", "next_session_cursor", "has_more"}
)
_REQUEST_FIELDS: Final = frozenset(
    {
        "model", "messages", "input", "prompt", "stream", "system", "max_tokens", "max_completion_tokens",
        "temperature", "top_p", "tools", "tool_choice", "response_format", "thinking",
    }
)
_COMPLETION_FIELDS: Final = frozenset(
    {
        "id", "object", "type", "created", "role", "content", "choices", "usage", "output", "stop_reason",
        "stop_sequence", "finish_reason", "model", "status", "delta", "index", "message", "tool_calls",
        "function", "name", "arguments", "tool_call_id", "call_id", "text", "refusal", "reasoning",
    }
)
_PRIVATE_ACTIVITY_FIELDS: Final = frozenset(
    {"api_base", "custom_llm_provider", "model", "model_id", "provider_model_name", "provider_specific_fields", "providers"}
)
_ACTIVITY_ALIAS_MAPS: Final = frozenset(
    {"models", "model_groups", "mcp_servers", "api_keys", "api_key_breakdown", "entities", "endpoints", "entity_total_api_keys"}
)
_PUBLIC_ERROR: Final = {
    "type": "api_error", "message": "Request failed. Use the request ID when contacting support.",
}
_USER_INFO_FIELDS: Final = frozenset(
    {
        "user_id", "user_email", "user_alias", "user_role", "max_budget", "model_max_budget", "models",
        "tpm_limit", "rpm_limit", "max_parallel_requests", "budget_duration", "budget_reset_at", "created_at",
        "updated_at", "sso_user_id", "teams", "organization_id", "blocked", "status",
    }
)
_TEAM_INFO_FIELDS: Final = frozenset(
    {
        "team_id", "team_alias", "models", "max_budget", "budget_duration", "budget_reset_at", "tpm_limit",
        "rpm_limit", "max_parallel_requests", "blocked", "organization_id", "created_at", "updated_at",
    }
)
_UI_PREFERENCE_FIELDS: Final = frozenset({"theme", "locale", "timezone", "density"})
_ACCOUNT_STATE_FIELDS: Final = frozenset({"password_reset_required", "email_verified", "onboarding_completed"})


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str):
        try:
            return _mapping(json.loads(value))
        except (ValueError, TypeError):
            return {}
    return {}


def _log_row(value: object) -> dict[str, object]:
    row: Final = _mapping(value)
    metadata: Final = _mapping(row.get("metadata"))
    public_model: Final = row.get("model_group")
    safe_metadata: Final = {key: item for key, item in metadata.items() if key in _LOG_METADATA_FIELDS}
    return {
        **{key: item for key, item in row.items() if key in _LOG_FIELDS},
        "model": public_model if isinstance(public_model, str) and public_model else None,
        "model_group": public_model if isinstance(public_model, str) and public_model else None,
        "metadata": {
            **safe_metadata,
            **({"error_information": _PUBLIC_ERROR} if metadata.get("error_information") else {}),
        },
    }


def customer_log_response(payload: _Payload, auth: UserAPIKeyAuth) -> _Payload | object:
    if not is_retail_customer(auth):
        return payload
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return [_log_row(row) for row in payload]
    page: Final = _mapping(payload)
    if "data" not in page:
        return _log_row(page) if page else payload
    rows: Final = page.get("data")
    return {
        **{key: item for key, item in page.items() if key in _PAGE_FIELDS},
        "data": [_log_row(row) for row in rows] if isinstance(rows, (list, tuple)) else [],
        "billing_source": "wallet_ledger",
    }


def _safe_request(value: object) -> dict[str, object]:
    request: Final = _mapping(value)
    body: Final = _mapping(request.get("body"))
    return {"body": {key: item for key, item in body.items() if key in _REQUEST_FIELDS}}


def _safe_completion(value: object, public_model: object = None, *, encoded: bool = True) -> object:
    if isinstance(value, str):
        if not encoded:
            return value
        try:
            return json.dumps(_safe_completion(json.loads(value), public_model, encoded=False))
        except (ValueError, TypeError):
            return None
    if isinstance(value, (list, tuple)):
        return [_safe_completion(item, public_model, encoded=False) for item in value]
    if not isinstance(value, Mapping):
        return value
    row: Final = _mapping(value)
    if row.get("error") or row.get("type") == "error":
        return {"error": _PUBLIC_ERROR}
    return {
        key: public_model if key == "model" else item if key == "content" else _safe_completion(
            item, public_model, encoded=False,
        )
        for key, item in row.items()
        if key in _COMPLETION_FIELDS or key.endswith("tokens") or key.endswith("tokens_details")
    }


def customer_log_detail(payload: _Payload, auth: UserAPIKeyAuth) -> _Payload | object:
    if not is_retail_customer(auth) or payload is None:
        return payload
    row: Final = _mapping(payload)
    request: Final = _safe_request(row.get("proxy_server_request"))
    public_model: Final = _mapping(request.get("body")).get("model", row.get("model_group"))
    return {
        **{key: item for key, item in row.items() if key in {"request_id", "session_id", "status"}},
        "messages": row.get("messages"),
        "response": _safe_completion(row.get("response"), public_model),
        "proxy_server_request": request,
    }


def private_activity_field(key: str) -> bool:
    lowered: Final = key.lower()
    return "spend" in lowered or "cost" in lowered or key in _PRIVATE_ACTIVITY_FIELDS


def public_activity_data(value: object, *, alias_map: bool = False) -> object:
    if isinstance(value, BaseModel):
        return public_activity_data(value.model_dump())
    if isinstance(value, (list, tuple)):
        return [public_activity_data(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    row: Final = _mapping(value)
    if alias_map:
        return {key: public_activity_data(item) for key, item in row.items()}
    public_groups: Final = row.get("model_groups")
    return {
        key: public_activity_data(public_groups if key == "models" else item, alias_map=key in _ACTIVITY_ALIAS_MAPS)
        for key, item in row.items()
        if not private_activity_field(key) and (key != "models" or isinstance(public_groups, Mapping))
    }


def customer_activity_response(payload: _Payload, auth: UserAPIKeyAuth) -> _Payload | JSONResponse:
    if not is_retail_customer(auth):
        return payload
    return JSONResponse(jsonable_encoder(public_activity_data(payload)), headers={"Cache-Control": "no-store"})


def _safe_user_info(value: object) -> dict[str, object]:
    row: Final = _mapping(value)
    metadata: Final = _mapping(row.get("metadata"))
    return {
        **{key: item for key, item in row.items() if key in _USER_INFO_FIELDS},
        "metadata": {
            **{key: item for key, item in metadata.items() if key in _UI_PREFERENCE_FIELDS and isinstance(item, str)},
            **{key: item for key, item in metadata.items() if key in _ACCOUNT_STATE_FIELDS and isinstance(item, bool)},
        },
    }


def _safe_team_info(value: object) -> dict[str, object]:
    row: Final = _mapping(value)
    members: Final = row.get("members_with_roles")
    return {
        **{key: item for key, item in row.items() if key in _TEAM_INFO_FIELDS},
        "members_with_roles": [
            {key: item for key, item in _mapping(member).items() if key in {"user_id", "user_email", "role"}}
            for member in members
        ] if isinstance(members, (list, tuple)) else [],
    }


def customer_user_info_response(payload: _Payload, auth: UserAPIKeyAuth) -> _Payload | JSONResponse:
    if not is_retail_customer(auth):
        return payload
    row: Final = _mapping(payload)
    keys: Final = row.get("keys")
    teams: Final = row.get("teams")
    public: Final = {
        "user_id": row.get("user_id"),
        "user_info": _safe_user_info(row.get("user_info")),
        "keys": [safe_customer_key_info(_mapping(key)) for key in keys] if isinstance(keys, (list, tuple)) else [],
        "teams": [_safe_team_info(team) for team in teams] if isinstance(teams, (list, tuple)) else [],
    } if "user_info" in row else _safe_user_info(row)
    return JSONResponse(jsonable_encoder(public), headers={"Cache-Control": "no-store"})
