import os
from collections.abc import Mapping
from typing import Final

from fastapi import HTTPException
from pydantic import JsonValue, TypeAdapter

_METADATA: Final = TypeAdapter(dict[str, JsonValue])
POLICY_KEY: Final = "retail_resource_policy"


def resource_policy_metadata(
    metadata: object, existing_metadata: object = None, *, provided: bool = True, owner_changed: bool = False,
) -> dict[str, JsonValue] | None:
    if not provided and not owner_changed:
        return None
    existing: Final = _METADATA.validate_python(existing_metadata) if isinstance(existing_metadata, Mapping) else {}
    incoming: Final = _METADATA.validate_python(metadata) if isinstance(metadata, Mapping) else {}
    retained: Final = existing if owner_changed and not provided else incoming
    if POLICY_KEY not in retained and POLICY_KEY in existing:
        return {**retained, POLICY_KEY: existing[POLICY_KEY]}
    return retained


async def validate_native_key_resource_metadata(
    metadata: object, user_id: str | None, existing_metadata: object = None,
    *, provided: bool = True, owner_changed: bool = False,
) -> dict[str, JsonValue] | None:
    merged: Final = resource_policy_metadata(
        metadata, existing_metadata, provided=provided, owner_changed=owner_changed,
    )
    if merged is None or POLICY_KEY not in merged:
        return merged
    if os.getenv("RETAIL_PORTAL_ENABLED", "false").lower() != "true":
        raise HTTPException(403, "Resource routing is unavailable while the retail portal is disabled")
    if not user_id:
        raise HTTPException(403, "Resource routing requires a key owned by a user account")
    from .pool_store import validate_key_resource_policy

    normalized: Final = await validate_key_resource_policy(merged[POLICY_KEY], user_id)
    return {**merged, POLICY_KEY: normalized}
