from __future__ import annotations

import json
import re
from typing import Any

from libs.attio.people import error_envelope

ERROR_CODE_TO_STATUS = {
    "validation_error": 400,
    "conflict": 409,
    "schema_mismatch": 422,
    "connectivity_error": 503,
    "configuration_error": 500,
    "unknown_error": 500,
}

STATUS_PATTERN = re.compile(r"\bStatus\s+(\d{3})\b")


class _LocalJSONResponse:
    """Minimal JSONResponse fallback for local tests without FastAPI installed."""

    def __init__(self, *, status_code: int, content: dict[str, Any]) -> None:
        self.status_code = status_code
        self.body = json.dumps(content).encode("utf-8")


def _build_json_response(*, status_code: int, content: dict[str, Any]):
    try:
        from fastapi.responses import JSONResponse
    except ModuleNotFoundError:
        return _LocalJSONResponse(status_code=status_code, content=content)
    return JSONResponse(status_code=status_code, content=content)


def _status_from_message(message: str | None) -> int | None:
    if not message:
        return None
    match = STATUS_PATTERN.search(message)
    if not match:
        return None
    parsed = int(match.group(1))
    if 400 <= parsed <= 599:
        return parsed
    return None


def status_code_from_error_payload(payload: dict[str, Any]) -> int:
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return 500

    first = errors[0]
    if not isinstance(first, dict):
        return 500

    message_status = _status_from_message(str(first.get("message", "")))
    if message_status:
        return message_status

    return ERROR_CODE_TO_STATUS.get(str(first.get("code", "")), 500)


def error_response_from_exception(error: Exception, *, strict: bool = False):
    envelope = error_envelope(error, strict=strict).model_dump()
    return _build_json_response(
        status_code=status_code_from_error_payload(envelope),
        content=envelope,
    )


def error_response_from_payload(payload: dict[str, Any]):
    return _build_json_response(
        status_code=status_code_from_error_payload(payload),
        content=payload,
    )
