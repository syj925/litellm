import json
import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from email.utils import parsedate_to_datetime
from typing import Final

from pydantic import JsonValue, TypeAdapter, ValidationError

_JSON: Final = TypeAdapter(JsonValue)
_MAX_FRAME_BYTES: Final = 16 * 1024 * 1024
_FRAME_BOUNDARY: Final = re.compile(rb"(\r\n\r\n|\n\n|\r\r)")
_NUMBER: Final = re.compile(rb"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,190}")
_PARAM: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.\[\]-]{0,127}")
_PUBLIC_PARAM_ROOTS: Final = frozenset(
    (
        "model messages input prompt temperature max_tokens max_completion_tokens max_output_tokens top_p top_k n "
        "stream stop seed frequency_penalty presence_penalty logprobs top_logprobs logit_bias response_format "
        "tools tool_choice functions function_call parallel_tool_calls reasoning reasoning_effort thinking service_tier "
        "metadata user store background previous_response_id conversation include truncation text instructions "
        "safety_identifier prompt_cache_key prompt_cache_retention modalities audio voice format output_format "
        "image images size quality output_compression input_fidelity mask aspect_ratio resolution negative_prompt "
        "stream_options timeout contents systeminstruction generationconfig generation_config safetysettings "
        "cachedcontent dimensions encoding_format documents query top_n return_documents max_chunks_per_doc "
        "file language timestamp_granularities purpose"
    ).split()
)
_PUBLIC_PARAM_FIELDS: Final = _PUBLIC_PARAM_ROOTS | frozenset(
    (
        "role content type text image_url url detail input_audio data format function name description parameters "
        "properties required additionalproperties strict arguments id call_id index tool_calls cache_control ttl effort "
        "budget_tokens source media_type mime_type imageurl inline_data inlinedata mimetype displayname file_data "
        "filedata file_uri fileuri parts thought thoughtsignature"
    ).split()
)
_CONFIGURATION: Final = re.compile(
    r"\b(?:pricing|expression|sql|database|catalog|driver|sales price|cache write prices?|usage bounds?|"
    r"configured bound|frozen context|reservation arithmetic|response attribute|usage fact|usage variable)\b",
    re.I,
)
_INTERNAL: Final = re.compile(
    r"https?://|\b(?:upstream|deployment|channel|api[_ -]?base|provider[_ -]?specific|fallback|router|"
    r"traceback|authorization|credential|bearer|api[_ -]?key|access[_ -]?token|secret|password)\b|"
    r"\b(?:litellm|\w*Exception|\w*Error)\b|\bsk-[A-Za-z0-9_-]+|\bAIza[A-Za-z0-9_-]+|"
    r"[A-Za-z]:\\|\b\d{1,3}(?:\.\d{1,3}){3}\b|"
    r"\b[A-Za-z0-9-]+\.(?:com|net|org|io|ai|cloud|local|internal|test|invalid)\b",
    re.I,
)
_CLIENT_MESSAGES: Final = frozenset(
    {
        "Insufficient wallet balance. Add funds or lower the output token limit",
        "Insufficient wallet balance for this request. Add funds or lower the output token limit",
        "Your wallet has reached its concurrent request limit",
    }
)
_BOUND: Final = r"-?[0-9]{1,9}(?:\.[0-9]{1,6})?"
_PARAMETER_MESSAGE: Final = re.compile(
    r"(?P<param>[A-Za-z_][A-Za-z0-9_.\[\]-]{0,127}) "
    r"(?:is required|must not be empty|must be "
    r"(?:positive|non-negative|nonnegative|an? (?:integer|number|string|boolean|array|object)"
    rf"(?: between {_BOUND} and {_BOUND})?|between {_BOUND} and {_BOUND}|at (?:least|most) {_BOUND}))\.?"
)
_MODEL_ACCESS: Final = re.compile(
    r"The requested model '[A-Za-z0-9_.:/+-]{1,128}' is not available for this API key, or the model name is invalid"
    r"(?:\. Check the models available to you and try again\.)?\.?"
)
_SAFE_TYPES: Final = frozenset(
    {
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "permission_denied",
        "not_found_error",
        "rate_limit_error",
        "overloaded_error",
        "api_error",
        "server_error",
        "timeout_error",
        "budget_exceeded",
        "context_length_exceeded",
        "request_too_large",
        "key_model_access_denied",
        "user_model_access_denied",
        "customer_model_access_denied",
        "team_model_access_denied",
        "org_model_access_denied",
        "project_model_access_denied",
        "expired_key",
        "auth_error",
        "content_policy_violation",
        "insufficient_quota",
    }
)
_SAFE_CODES: Final = _SAFE_TYPES | frozenset(
    {
        "invalid_api_key",
        "invalid_parameter",
        "invalid_value",
        "missing_required_parameter",
        "model_not_found",
        "model_not_allowed",
        "insufficient_funds",
        "insufficient_balance",
        "max_tokens_exceeded",
        "max_output_tokens_exceeded",
        "rate_limit_exceeded",
        "unsupported_value",
        "unsupported_parameter",
        "billing_hard_limit_reached",
        "string_too_long",
        "invalid_image",
        "invalid_image_format",
        "invalid_image_url",
        "invalid_json",
    }
)
_GEMINI_STATUS: Final = frozenset(
    {
        "INVALID_ARGUMENT",
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "NOT_FOUND",
        "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION",
        "ABORTED",
        "OUT_OF_RANGE",
        "UNIMPLEMENTED",
        "INTERNAL",
        "UNAVAILABLE",
        "DEADLINE_EXCEEDED",
        "CANCELLED",
        "UNKNOWN",
    }
)
_STATUS_BY_TYPE: Final = {
    "invalid_request_error": 400,
    "context_length_exceeded": 400,
    "authentication_error": 401,
    "invalid_api_key": 401,
    "permission_error": 403,
    "permission_denied": 403,
    "not_found_error": 404,
    "rate_limit_error": 429,
    "rate_limit_exceeded": 429,
    "insufficient_quota": 429,
    "overloaded_error": 503,
    "timeout_error": 504,
    "budget_exceeded": 402,
    "insufficient_funds": 402,
    "insufficient_balance": 402,
    "model_not_allowed": 403,
    "model_not_found": 404,
}
_STATUS_BY_SYMBOL: Final = {
    "INVALID_ARGUMENT": 400,
    "UNAUTHENTICATED": 401,
    "PERMISSION_DENIED": 403,
    "NOT_FOUND": 404,
    "RESOURCE_EXHAUSTED": 429,
    "FAILED_PRECONDITION": 400,
    "ABORTED": 409,
    "OUT_OF_RANGE": 400,
    "UNIMPLEMENTED": 501,
    "INTERNAL": 500,
    "UNAVAILABLE": 503,
    "DEADLINE_EXCEEDED": 504,
    "CANCELLED": 499,
    "UNKNOWN": 500,
    "error": 500,
    "failed": 500,
}
_NORMAL_PAYLOAD_FIELDS: Final = frozenset({"choices", "candidates", "output", "delta", "content", "response"})
_PUBLIC_HEADERS: Final = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "content-disposition",
        "content-language",
        "cache-control",
        "pragma",
        "expires",
        "etag",
        "last-modified",
        "vary",
        "date",
        "retry-after",
        "access-control-allow-origin",
        "access-control-allow-credentials",
        "access-control-allow-methods",
        "access-control-allow-headers",
        "access-control-max-age",
        "access-control-expose-headers",
        "x-accel-buffering",
        "x-content-type-options",
        "x-frame-options",
        "referrer-policy",
        "strict-transport-security",
        "x-litellm-key-tpm-limit",
        "x-litellm-key-rpm-limit",
        "x-litellm-key-max-budget",
        "x-litellm-response-duration-ms",
    }
)
_REQUEST_HEADERS: Final = frozenset({"x-request-id", "x-litellm-call-id"})
_PRIVATE_FIELDS: Final = frozenset(
    {
        "provider_specific_fields",
        "headers",
        "upstream_request_id",
        "api_base",
        "api_base_origin",
        "deployment",
        "deployment_id",
        "model_id",
        "channel",
        "channel_id",
        "traceback",
        "_hidden_params",
        "cost",
        "spend",
        "provider_cost",
        "original_cost",
        "margin_amount",
        "margin_percent",
        "cost_breakdown",
        "model_info",
        "litellm_params",
        "proxy_server_request",
        "litellm_metadata",
    }
)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _request_id(value: str | None) -> str | None:
    return value if value is not None and _ID.fullmatch(value) is not None else None


def customer_response_headers(
    headers: Sequence[tuple[bytes, bytes]],
    *,
    request_id: str | None = None,
) -> tuple[tuple[bytes, bytes], ...]:
    identifier: Final = _request_id(request_id)
    kept: Final = tuple(
        (key, _exposed_headers(value) if key.lower() == b"access-control-expose-headers" else value)
        for key, value in headers
        if _public_header(key, value)
    )
    identifiers: Final = (
        (
            (b"x-request-id", identifier.encode("ascii")),
            (b"x-litellm-call-id", identifier.encode("ascii")),
        )
        if identifier is not None
        else ()
    )
    return (*kept, *identifiers)


def _public_header(key: bytes, value: bytes) -> bool:
    name: Final = key.decode("latin-1").lower()
    if name not in _PUBLIC_HEADERS or b"\r" in value or b"\n" in value:
        return False
    if name.startswith("x-litellm-"):
        return _NUMBER.fullmatch(value) is not None
    if name == "retry-after":
        if _NUMBER.fullmatch(value) is not None:
            return True
        try:
            return parsedate_to_datetime(value.decode("ascii")) is not None
        except (ValueError, TypeError, UnicodeError, OverflowError):
            return False
    return True


def _exposed_headers(value: bytes) -> bytes:
    return ", ".join(
        name.strip()
        for name in value.decode("latin-1").split(",")
        if name.strip().lower() in _PUBLIC_HEADERS | _REQUEST_HEADERS | {"x-retail-trace-id"}
    ).encode("latin-1")


def _http_status(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599:
        return value
    if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isdigit() and 400 <= int(value) <= 599:
        return int(value)
    return None


def _status(error: Mapping[str, object], status_code: int) -> int:
    if 400 <= status_code <= 599:
        return status_code
    numeric: Final = next(
        (
            _http_status(error.get(key))
            for key in ("code", "status_code", "status")
            if _http_status(error.get(key)) is not None
        ),
        None,
    )
    if numeric is not None:
        return numeric
    code: Final = error.get("code")
    kind: Final = error.get("type")
    if isinstance(kind, str) and kind in _STATUS_BY_TYPE:
        return _STATUS_BY_TYPE[kind]
    symbolic: Final = error.get("status")
    if isinstance(symbolic, str) and symbolic in _STATUS_BY_SYMBOL:
        return _STATUS_BY_SYMBOL[symbolic]
    if isinstance(code, str) and code in _SAFE_CODES:
        return _STATUS_BY_TYPE.get(
            code,
            403 if code.endswith("_model_access_denied") else (500 if code in {"server_error", "api_error"} else 400),
        )
    return 500


def _bare_error(document: Mapping[str, object]) -> bool:
    if not isinstance(document.get("message"), str) or _NORMAL_PAYLOAD_FIELDS.intersection(document):
        return False
    code: Final = document.get("code")
    status: Final = document.get("status")
    return (
        any(_http_status(document.get(key)) is not None for key in ("code", "status_code", "status"))
        or isinstance(code, str)
        and code in _SAFE_CODES
        or isinstance(status, str)
        and status in _STATUS_BY_SYMBOL
    )


def _default_message(status_code: int) -> str:
    if status_code in {400, 422}:
        return "The request contains invalid or unsupported parameters. Check the indicated parameter and try again."
    if status_code == 401:
        return "Authentication failed. Check your API key and try again."
    if status_code == 402:
        return "Insufficient wallet balance for this request. Add funds or lower the output token limit."
    if status_code == 403:
        return "You do not have permission to use this model or operation. Check your model access."
    if status_code == 404:
        return "The requested model or resource was not found. Check its name and your access."
    if status_code == 413:
        return "The request is too large. Reduce its size and try again."
    if status_code == 429:
        return "The request limit has been reached. Retry later or reduce concurrent requests."
    if status_code in {408, 504}:
        return "The model request timed out. Retry later or reduce the request size."
    if status_code in {502, 503}:
        return "The requested model is temporarily unavailable. Retry later."
    return "The request could not be completed. Retry later and contact support with your request ID if it continues."


def _default_type(status_code: int) -> str:
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        402: "budget_exceeded",
        403: "permission_error",
        404: "not_found_error",
        408: "timeout_error",
        413: "request_too_large",
        422: "invalid_request_error",
        429: "rate_limit_error",
        503: "overloaded_error",
        504: "timeout_error",
    }.get(status_code, "api_error")


def _safe_param(value: object) -> str | None:
    if not isinstance(value, str) or not _PARAM.fullmatch(value) or _INTERNAL.search(value):
        return None
    parts: Final = tuple(part for part in re.split(r"[.\[\]]", value) if part)
    return (
        value
        if parts[0].lower() in _PUBLIC_PARAM_ROOTS
        and all(
            (part.isascii() and part.isdigit() and len(part) <= 6) or part.lower() in _PUBLIC_PARAM_FIELDS
            for part in parts
        )
        and not any(part.lower() in _PRIVATE_FIELDS for part in parts)
        else None
    )


def _message(value: object, status_code: int, param: str | None, code: object) -> str:
    if not isinstance(value, str) or not 400 <= status_code < 500 or status_code == 401:
        return _default_message(status_code)
    normalized: Final = " ".join(value.split())
    if (
        status_code == 403
        and _MODEL_ACCESS.fullmatch(normalized) is not None
        and not _INTERNAL.search(normalized.replace("API key", "account"))
    ):
        return normalized
    if _CONFIGURATION.search(normalized):
        return "This model cannot complete the request right now. Contact support with your request ID."
    if status_code in {402, 429} and normalized.removesuffix(".") in _CLIENT_MESSAGES:
        return normalized
    match: Final = _PARAMETER_MESSAGE.fullmatch(normalized) if status_code in {400, 422} else None
    if match is not None and _safe_param(match.group("param")) is not None:
        return normalized
    if status_code in {400, 422} and param is not None:
        return (
            f"Parameter '{param}' is required."
            if code == "missing_required_parameter"
            else (f"Invalid or unsupported value for parameter '{param}'. Check its type and range.")
        )
    return _default_message(status_code)


def _error_object(error: Mapping[str, object], status_code: int, request_id: str | None) -> Mapping[str, JsonValue]:
    kind: Final = error.get("type")
    code: Final = error.get("code")
    public_code: Final[JsonValue] = (
        code
        if (isinstance(code, str) and (code in _SAFE_CODES or code == str(status_code)))
        or (isinstance(code, int) and not isinstance(code, bool) and code == status_code)
        else str(status_code)
    )
    identifier: Final = _request_id(request_id)
    gemini: Final = error.get("status")
    param: Final = _safe_param(error.get("param"))
    if status_code == 429 and kind == "budget_exceeded" and param == "model":
        return {
            "message": (
                "Your account budget for this model cannot cover this request. Reduce the request size or contact "
                "the account administrator to review the budget window."
            ),
            "type": "budget_exceeded",
            "param": "model",
            "code": "budget_exceeded",
            **({"request_id": identifier} if identifier is not None else {}),
        }
    return {
        "message": _message(error.get("message"), status_code, param, code),
        "type": kind if isinstance(kind, str) and kind in _SAFE_TYPES else _default_type(status_code),
        "param": param,
        "code": public_code,
        **({"status": gemini} if isinstance(gemini, str) and gemini in _GEMINI_STATUS else {}),
        **({"request_id": identifier} if identifier is not None else {}),
    }


def _json(value: object) -> JsonValue:
    return _JSON.validate_python(value)


def customer_error_payload(
    payload: object,
    *,
    status_code: int,
    request_id: str | None = None,
) -> JsonValue | None:
    document: Final = _mapping(payload)
    response: Final = _mapping(document.get("response"))
    is_responses_error: Final = document.get("type") == "response.failed" or (
        document.get("type") == "response.incomplete" and bool(response.get("error"))
    )
    raw_error: Final = (
        response.get("error") if is_responses_error else (document if _bare_error(document) else document.get("error"))
    )
    is_error: Final = document.get("type") == "error" or is_responses_error
    if not raw_error and not is_error and status_code < 400:
        return None
    detail: Final = document.get("detail")
    candidate: Final = raw_error or _mapping(detail).get("error") or detail or (document if is_error else None)
    source: Final = _mapping(candidate)
    error: Final = source or {"message": candidate if isinstance(candidate, str) else None}
    resolved_status: Final = _status(error, status_code)
    safe: Final = dict(_error_object(error, resolved_status, request_id))
    identifier: Final = _request_id(request_id)
    if is_responses_error:
        return _json(
            {
                "type": document["type"],
                **(
                    {"sequence_number": document["sequence_number"]}
                    if isinstance(document.get("sequence_number"), int)
                    else {}
                ),
                "response": {**_clean_envelope(response), "error": safe},
            }
        )
    return _json(
        {
            **({"type": "error"} if document.get("type") == "error" else {}),
            "error": safe,
            **({"request_id": identifier} if identifier is not None else {}),
        }
    )


def _clean_envelope(payload: Mapping[str, object]) -> Mapping[str, object]:
    clean: Final = {key: value for key, value in payload.items() if key not in _PRIVATE_FIELDS}
    usage: Final = clean.get("usage")
    return {
        **clean,
        **(
            {"usage": {key: value for key, value in usage.items() if key not in _PRIVATE_FIELDS}}
            if isinstance(usage, Mapping)
            else {}
        ),
    }


def _clean_choice(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    clean: Final = _clean_envelope(value)
    return {
        **clean,
        **{
            key: _clean_envelope(_mapping(clean[key]))
            for key in ("delta", "message")
            if isinstance(clean.get(key), Mapping)
        },
    }


def customer_response_payload(
    payload: object,
    *,
    status_code: int,
    request_id: str | None = None,
) -> JsonValue | None:
    error: Final = customer_error_payload(payload, status_code=status_code, request_id=request_id)
    if error is not None:
        return error
    document: Final = _mapping(payload)
    clean: Final = _clean_envelope(document)
    choices: Final = clean.get("choices")
    response: Final = clean.get("response")
    candidates: Final = clean.get("candidates")
    changed: Final = {
        **clean,
        **({"choices": [_clean_choice(value) for value in choices]} if isinstance(choices, list) else {}),
        **({"response": _clean_envelope(response)} if isinstance(response, Mapping) else {}),
        **({"candidates": [_clean_choice(value) for value in candidates]} if isinstance(candidates, list) else {}),
    }
    return _json(changed) if changed != document else None


def customer_error_body(*, status_code: int, request_id: str | None = None) -> bytes:
    return json.dumps(
        customer_error_payload({}, status_code=status_code, request_id=request_id),
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_sse_frame(frame: bytes, status_code: int, request_id: str | None) -> bytes:
    lines: Final = frame.splitlines(keepends=True)
    events: Final = tuple(line[6:].strip() for line in lines if line.startswith(b"event:"))
    error_event: Final = any(event in {b"error", b"response.failed", b"response.incomplete"} for event in events)
    data: Final = b"\n".join(line[5:].strip() for line in lines if line.startswith(b"data:"))
    if not data and error_event:
        return (
            b"event: error\ndata: "
            + customer_error_body(
                status_code=status_code if status_code >= 400 else 500,
                request_id=request_id,
            )
            + b"\n\n"
        )
    if not data or data in {b"[DONE]", b"ping", b"heartbeat", b"keepalive", b"keep-alive"}:
        return frame
    try:
        payload: Final[object] = json.loads(data)
        recognized_error: Final = customer_error_payload(payload, status_code=status_code, request_id=request_id)
        replacement: Final = (
            customer_error_payload({"error": payload}, status_code=status_code, request_id=request_id)
            if error_event and recognized_error is None
            else recognized_error
            if recognized_error is not None
            else customer_response_payload(payload, status_code=status_code, request_id=request_id)
        )
    except (ValueError, UnicodeError, ValidationError):
        return (
            b"event: error\ndata: "
            + customer_error_body(
                status_code=status_code if status_code >= 400 else 500,
                request_id=request_id,
            )
            + b"\n\n"
        )
    if replacement is None:
        return frame
    event: Final = (
        b"".join(
            line
            for line in lines
            if line.startswith(b"event:")
            and line[6:].strip()
            in {
                b"error",
                b"response.failed",
                b"response.incomplete",
            }
        )
        if error_event or _mapping(payload).get("error")
        else b"".join(line for line in lines if line.startswith(b"event:"))
    )
    return event + b"data: " + json.dumps(replacement, separators=(",", ":")).encode("utf-8") + b"\n\n"


def _frame_boundaries(data: bytes) -> Iterator[int]:
    return (match.end() for match in _FRAME_BOUNDARY.finditer(data))


class CustomerSSEPrivacy:
    def __init__(
        self,
        *,
        status_code: int = 200,
        request_id: str | None = None,
        max_frame_bytes: int = _MAX_FRAME_BYTES,
        _scan: Callable[[bytes], Iterable[int]] = _frame_boundaries,
    ) -> None:
        if max_frame_bytes <= 0:
            raise ValueError("SSE frame limit must be positive")
        self._status_code = status_code
        self._request_id = request_id
        self._max_frame_bytes = max_frame_bytes
        self._scan = _scan
        self._pending = bytearray()
        self._tail = b""
        self._discarding = False

    def feed(self, chunk: bytes, *, final: bool = False) -> bytes:
        combined: Final = self._tail + chunk
        prior_tail_bytes: Final = len(self._tail)
        output: Final = deque[bytes]()
        offset = 0  # rebind-ok: advance once through the new chunk's frame boundaries
        last_boundary = 0  # rebind-ok: retain only the unconsumed cross-chunk delimiter suffix
        for boundary in self._scan(combined):
            end: Final = boundary - prior_tail_bytes
            frame: Final = self._frame(chunk[offset:end])
            if frame:
                output.append(frame)
            offset = end
            last_boundary = boundary
        tail_overflow: Final = self._append(chunk[offset:]) if offset < len(chunk) else b""
        if tail_overflow:
            output.append(tail_overflow)
        self._tail = combined[max(last_boundary, len(combined) - 3) :]
        if final:
            final_frame: Final = self._complete_frame()
            if final_frame:
                output.append(final_frame)
            self._tail = b""
        return b"".join(output)

    def _frame(self, piece: bytes) -> bytes:
        if not self._pending and not self._discarding:
            return (
                self._overflow()
                if len(piece) > self._max_frame_bytes
                else _safe_sse_frame(
                    piece,
                    self._status_code,
                    self._request_id,
                )
            )
        overflow: Final = self._append(piece)
        return overflow + self._complete_frame()

    def _append(self, piece: bytes) -> bytes:
        if self._discarding or not piece:
            return b""
        if len(self._pending) + len(piece) > self._max_frame_bytes:
            self._pending.clear()
            self._discarding = True
            return self._overflow()
        self._pending.extend(piece)
        return b""

    def _complete_frame(self) -> bytes:
        if self._discarding:
            self._discarding = False
            return b""
        frame: Final = bytes(self._pending)
        self._pending.clear()
        return _safe_sse_frame(frame, self._status_code, self._request_id) if frame else b""

    def _overflow(self) -> bytes:
        return b"data: " + customer_error_body(status_code=502, request_id=self._request_id) + b"\n\n"
