from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from obrabot4.protocol import ProtocolError


PROFILE_AUDIT_WORKER_PROTOCOL = 1
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_ERROR_BYTES = 64 * 1024
_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_QUOTA_ERROR_CODES = frozenset(
    {"insufficient_quota", "billing_hard_limit_reached"}
)
ResponsesTransport = Callable[[dict[str, Any], str, float], dict[str, Any]]


class ResponsesAPIError(RuntimeError):
    """A sanitized upstream failure safe to send through the wire protocol."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        error_type: str | None = None,
    ) -> None:
        safe_code = code if _SAFE_CODE.fullmatch(code) else "openai_error"
        safe_type = (
            error_type
            if isinstance(error_type, str) and _SAFE_CODE.fullmatch(error_type)
            else None
        )
        super().__init__(safe_code)
        self.code = safe_code
        self.status_code = status_code
        self.retryable = retryable
        self.error_type = safe_type


def _safe_error_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if _SAFE_CODE.fullmatch(normalized) else None


def _retryable_status(status_code: int | None) -> bool:
    return status_code in {408, 409, 429} or bool(
        status_code is not None and 500 <= status_code <= 599
    )


def _http_error_fields(error: urllib.error.HTTPError) -> tuple[str, str | None]:
    try:
        body = error.read(MAX_ERROR_BYTES + 1)
    except Exception:
        return "openai_http_error", None
    if len(body) > MAX_ERROR_BYTES:
        return "openai_http_error", None
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "openai_http_error", None
    raw_error = document.get("error") if isinstance(document, Mapping) else None
    if not isinstance(raw_error, Mapping):
        return "openai_http_error", None
    code = _safe_error_value(raw_error.get("code"))
    error_type = _safe_error_value(raw_error.get("type"))
    return code or error_type or "openai_http_error", error_type


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    return isinstance(getattr(error, "reason", None), (TimeoutError, socket.timeout))


def default_responses_transport(
    payload: dict[str, Any], api_key: str, timeout: float
) -> dict[str, Any]:
    """Call Responses API without logging request data or credentials."""

    if payload.get("store") is not False:
        raise ResponsesAPIError("openai_store_must_be_false")
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "obrabot4/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        code, error_type = _http_error_fields(error)
        raise ResponsesAPIError(
            code,
            status_code=error.code,
            retryable=(
                code not in _QUOTA_ERROR_CODES and _retryable_status(error.code)
            ),
            error_type=error_type,
        ) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        raise ResponsesAPIError(
            "openai_timeout" if _is_timeout(error) else "openai_connection_error",
            retryable=True,
        ) from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ResponsesAPIError("invalid_openai_response", retryable=True)
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ResponsesAPIError("invalid_openai_response", retryable=True) from None
    if not isinstance(document, dict):
        raise ResponsesAPIError("invalid_openai_response", retryable=True)
    return document


def execute_openai_wire_request(
    request: Mapping[str, Any],
    *,
    api_key: str,
    transport: ResponsesTransport = default_responses_transport,
) -> dict[str, Any]:
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not _SAFE_REQUEST_ID.fullmatch(request_id):
        raise ProtocolError("wire_request_id_invalid")
    if (
        request.get("type") != "openai_request"
        or request.get("protocol") != PROFILE_AUDIT_WORKER_PROTOCOL
    ):
        raise ProtocolError("wire_request_invalid")
    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise ProtocolError("wire_payload_invalid")
    timeout = request.get("timeout_seconds")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not 3.0 <= float(timeout) <= 120.0
    ):
        raise ProtocolError("wire_timeout_invalid")
    try:
        response = transport(payload, api_key, float(timeout))
    except ResponsesAPIError as error:
        return {
            "type": "openai_error",
            "protocol": PROFILE_AUDIT_WORKER_PROTOCOL,
            "request_id": request_id,
            "code": error.code,
            "status_code": error.status_code,
            "retryable": error.retryable,
            "error_type": error.error_type,
        }
    return {
        "type": "openai_response",
        "protocol": PROFILE_AUDIT_WORKER_PROTOCOL,
        "request_id": request_id,
        "response": response,
    }
