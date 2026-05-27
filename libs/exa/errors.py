"""Exa API error classes with HTTP status mapping."""

from __future__ import annotations

from typing import Any


class ExaError(Exception):
    """Base exception for Exa API errors."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        request_id: str | None = None,
        body: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.request_id = request_id
        self.body = body


class ExaAuthError(ExaError):
    """401 Unauthorized — invalid or missing API key."""

    pass


class ExaBadRequestError(ExaError):
    """400/422 Bad Request — invalid parameters or schema."""

    pass


class ExaRateLimitError(ExaError):
    """429 Too Many Requests — rate limit exceeded."""

    pass


class ExaServerError(ExaError):
    """5xx Server Error — Exa service error."""

    pass


def from_http_status(
    status: int,
    body: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> ExaError:
    """Map HTTP status code to typed exception.

    Args:
        status: HTTP status code.
        body: Response body (optional). Expected to be a dict, but non-dict
            payloads (lists, strings, ``None``) are tolerated and treated as
            "no message" rather than crashing the translation path.
        request_id: Request ID from response (optional).

    Returns:
        Appropriate ExaError subclass instance.
    """
    # ``body`` ought to be a dict but real-world error responses sometimes
    # come back as plain text or arrays. Normalize to a safe shape so the
    # translation never raises a new exception and masks the original HTTP
    # failure (roborev finding).
    safe_body: dict[str, Any] = body if isinstance(body, dict) else {}
    message = safe_body.get("message", "")
    if not message:
        message = {
            401: "Unauthorized — invalid or missing API key",
            400: "Bad Request — invalid parameters",
            422: "Unprocessable Entity — schema validation failed",
            429: "Too Many Requests — rate limit exceeded",
        }.get(status, f"HTTP {status}")

    if status == 401:
        return ExaAuthError(message, status=status, request_id=request_id, body=body)
    elif status in (400, 422):
        return ExaBadRequestError(
            message,
            status=status,
            request_id=request_id,
            body=body,
        )
    elif status == 429:
        return ExaRateLimitError(
            message,
            status=status,
            request_id=request_id,
            body=body,
        )
    elif 500 <= status < 600:
        return ExaServerError(message, status=status, request_id=request_id, body=body)
    else:
        return ExaError(message, status=status, request_id=request_id, body=body)
