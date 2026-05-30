"""Fathom API client adapter.

Wraps the official ``fathom-python`` SDK (import name ``fathom_python``, a
Speakeasy-generated client) so the rest of the codebase talks to Fathom through
idiomatic helpers instead of the SDK surface directly.

The webhook ingest path (``src/fathom/webhook/call.py``) does NOT need the SDK —
Fathom POSTs recordings to us. This adapter exists for the *pull* direction:
listing recordings via the REST API to backfill records that predate the
webhook. The SDK import is therefore lazy so ``import libs.fathom`` stays cheap
and never hard-requires the SDK at module load.

Key resolution order for :func:`get_client` mirrors ``libs/attio/client.py``:

1. Explicit ``api_key`` argument (tests, one-off scripts).
2. The contextvar set by :func:`api_key_scope`.
3. ``os.environ["FATHOM_API_KEY"]`` (the path a script under ``infisical run``
   uses, matching ``cli/granola``'s ``GRANOLA_API_KEY`` convention).
"""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from libs.fathom.errors import FathomAuthError

if TYPE_CHECKING:
    from fathom_python.models import Meeting

_api_key_var: ContextVar[str | None] = ContextVar("fathom_api_key", default=None)


@contextmanager
def api_key_scope(api_key: str) -> Generator[None, None, None]:
    """Bind ``api_key`` as the active Fathom key for this async/sync context."""
    token = _api_key_var.set(api_key)
    try:
        yield
    finally:
        _api_key_var.reset(token)


def get_client(api_key: str | None = None) -> Any:
    """Build a Fathom SDK client, resolving the API key from the 3-tier chain."""
    token = (
        api_key or _api_key_var.get() or os.environ.get("FATHOM_API_KEY", "")
    ).strip()
    if not token:
        raise FathomAuthError(
            "Fathom API key not resolved. Provide one of: "
            "(1) explicit api_key= argument, "
            "(2) call inside libs.fathom.client.api_key_scope(...), "
            "(3) set FATHOM_API_KEY in the process environment.",
        )
    # Lazy import: keeps the SDK an optional dependency for the webhook path.
    from fathom_python import Fathom, models

    return Fathom(security=models.Security(api_key_auth=token))


def iter_meetings(
    *,
    created_after: str | None = None,
    created_before: str | None = None,
    recorded_by: list[str] | None = None,
    include_summary: bool = True,
    include_action_items: bool = True,
    include_transcript: bool = False,
    client: Any | None = None,
) -> Iterator[Meeting]:
    """Yield every Fathom meeting matching the filters, paging transparently.

    Pagination follows the SDK's cursor model: ``list_meetings`` returns a
    response whose ``.next()`` fetches the following page (or ``None`` at the
    end) — page size is server-controlled, there is no client-side limit param.
    ``transcript`` defaults off — backfilling Meetings + summary/action notes
    never reads it, and omitting it keeps payloads small under Fathom's
    60-calls / 60-seconds rate limit.

    ``client`` is injectable so tests can pass a stub without a live key.
    """
    fathom = client if client is not None else get_client()

    kwargs: dict[str, Any] = {
        "include_summary": include_summary,
        "include_action_items": include_action_items,
        "include_transcript": include_transcript,
    }
    if created_after is not None:
        kwargs["created_after"] = created_after
    if created_before is not None:
        kwargs["created_before"] = created_before
    if recorded_by:
        kwargs["recorded_by"] = recorded_by

    response = fathom.list_meetings(**kwargs)
    while response is not None:
        for meeting in response.result.items:
            yield meeting
        response = response.next()
