"""Exa SDK client builder.

Key resolution order for ``_get_client()``:

1. Explicit ``api_key`` argument (used by tests and one-off CLI scripts).
2. The contextvar set by :func:`api_key_scope` — webhook and ``src/app.py``
   call sites open this scope after fetching the key from Infisical.
3. ``os.environ["EXA_API_KEY"]`` — back-compat fallback for any path
   that still relies on the legacy named Modal Secret.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar


class ExaAPIKeyMissingError(ValueError):
    """Raised by :func:`_get_client` when no Exa API key can be resolved.

    Subclasses ``ValueError`` for back-compat with callers that catch the
    legacy string-shaped error; existing ``except ValueError`` handlers
    keep working. New code (e.g. the Modal wrapper) can ``isinstance``-
    branch on this dedicated type to attach an Infisical remediation hint
    without doing a substring match on the error message.
    """


_api_key_var: ContextVar[str | None] = ContextVar(
    "exa_api_key",
    default=None,
)


@contextmanager
def api_key_scope(api_key: str) -> Generator[None, None, None]:
    """Bind ``api_key`` as the active Exa key for this async/sync context.

    Mirrors :func:`libs.parallel.client.api_key_scope`. The scope is reset on
    exit so concurrent Modal inputs in the same container do not see each
    other's keys.
    """
    token = _api_key_var.set(api_key)
    try:
        yield
    finally:
        _api_key_var.reset(token)


def _get_client(api_key: str | None = None):  # pyright: ignore[reportUnusedFunction]
    # Import here to avoid namespace collision with src/exa
    import exa_py

    token = (api_key or _api_key_var.get() or os.environ.get("EXA_API_KEY", "")).strip()
    if not token:
        raise ExaAPIKeyMissingError(
            "Exa API key not resolved. Provide one of: "
            "(1) explicit api_key= argument, "
            "(2) call inside libs.exa.client.api_key_scope(...), "
            "(3) set EXA_API_KEY in the process environment.",
        )
    return exa_py.Exa(api_key=token)
