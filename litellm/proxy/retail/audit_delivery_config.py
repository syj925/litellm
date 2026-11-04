import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Final, Literal, TypeAlias
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    url: str = field(repr=False)
    secret: bytes = field(repr=False)
    fingerprint: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class WebhookUnavailable:
    code: Literal["not_configured", "invalid_configuration"]


WebhookConfiguration: TypeAlias = WebhookConfig | WebhookUnavailable


def _endpoint(value: str, allow_http: bool) -> str | None:
    if len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value) or "?" in value or "#" in value:
        return None
    try:
        parsed: Final = urlsplit(value)
        host: Final = parsed.hostname
        port: Final = parsed.port
        if host is None or parsed.username is not None or parsed.password is not None:
            return None
        if parsed.scheme != "https" and not (allow_http and parsed.scheme == "http"):
            return None
        normalized_host: Final = host.encode("idna").decode("ascii").lower()
        address: Final = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
        authority: Final = address if port is None else f"{address}:{port}"
        return urlunsplit((parsed.scheme, authority, parsed.path or "/", "", ""))
    except (ValueError, UnicodeError):
        return None


def _secret(value: str) -> bytes | None:
    if not value.startswith("whsec_") or len(value) > 512:
        return None
    try:
        decoded: Final = base64.b64decode(value[6:].encode("ascii"), validate=True)
    except (binascii.Error, UnicodeError):
        return None
    return decoded if 32 <= len(decoded) <= 64 else None


def webhook_configuration(environment: Mapping[str, str] | None = None) -> WebhookConfiguration:
    selected: Final = os.environ if environment is None else environment
    source_url: Final = selected.get("RETAIL_AUDIT_WEBHOOK_URL", "")
    source_secret: Final = selected.get("RETAIL_AUDIT_WEBHOOK_SECRET", "")
    if not source_url and not source_secret:
        return WebhookUnavailable("not_configured")
    url: Final = _endpoint(source_url, selected.get("RETAIL_AUDIT_WEBHOOK_ALLOW_HTTP", "false").lower() == "true")
    secret: Final = _secret(source_secret)
    if url is None or secret is None:
        return WebhookUnavailable("invalid_configuration")
    fingerprint: Final = sha256(url.encode("utf-8") + b"\x00" + secret).hexdigest()
    return WebhookConfig(url, secret, fingerprint)
