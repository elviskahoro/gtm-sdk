"""One JSON line per event, always on, never raises.

Modal renders stdout into its dashboard verbatim, so every call here becomes
one line of structured data we can filter on. The standard fields (`ts`,
`event`, `source`, `request_id`) come from contextvars set once per request
by `webhook_request_context`; callers add their own with `**fields`.

The logger must not be the reason a request fails — `log()` swallows all
serialisation errors and falls back to a minimal `{event, error}` line.
"""

from __future__ import annotations

import contextlib
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import orjson
from uuid_extensions import uuid7

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import Request


_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)
_SOURCE: ContextVar[str | None] = ContextVar("source", default=None)

_REQUEST_ID_HEADER = "x-request-id"


def set_source(source: str) -> None:
    """Set the `source` contextvar. Called once at module import per webhook file."""
    _SOURCE.set(source)


def set_request_id(request_id: str) -> None:
    """Set the `request_id` contextvar. Normally driven by `webhook_request_context`."""
    _REQUEST_ID.set(request_id)


def get_request_id() -> str | None:
    return _REQUEST_ID.get()


def get_source() -> str | None:
    return _SOURCE.get()


def extract_or_generate_request_id(request: Request) -> str:
    """Return the inbound `X-Request-Id` header, falling back to a fresh uuid7."""
    header_value = request.headers.get(_REQUEST_ID_HEADER)
    if header_value:
        return header_value
    return str(uuid7())


def log(event: str, **fields: Any) -> None:
    """Emit one JSON line to stdout. Never raises."""
    try:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "source": _SOURCE.get(),
            "request_id": _REQUEST_ID.get(),
        }
        payload.update(fields)
        line = orjson.dumps(payload).decode("utf-8")
    except (TypeError, ValueError) as exc:
        # orjson refused something in `fields`. Emit a fallback so the event
        # is still visible without taking down the request.
        try:
            line = orjson.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "event": event,
                    "source": _SOURCE.get(),
                    "request_id": _REQUEST_ID.get(),
                    "log_error": f"{type(exc).__name__}: {exc}",
                },
            ).decode("utf-8")
        except Exception:  # noqa: BLE001 - last-resort fallback, never raise from log()
            return

    # sys.stdout.write + flush rather than print() so a caller monkeypatching
    # print() can't accidentally recurse into us.
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - last-resort fallback, never raise from log()
        return


@contextlib.contextmanager
def webhook_request_context(request: Request) -> Generator[str, None, None]:
    """Bind `request_id` for the duration of a webhook call.

    Reads `X-Request-Id` from the inbound request (uuid7 fallback), sets the
    contextvar, yields the id so the caller can log it, and resets the
    contextvar on exit so a recycled Modal container starts clean.
    """
    request_id = extract_or_generate_request_id(request)
    token = _REQUEST_ID.set(request_id)
    try:
        yield request_id
    finally:
        _REQUEST_ID.reset(token)
