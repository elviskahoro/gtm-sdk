"""Sanity Query API client.

Wraps the Sanity Content Lake GROQ Query API
(``GET /{apiVersion}/data/query/{dataset}``). The dlthub blog dataset is
**public**, so reads work with no auth — the bearer token is only needed for
private datasets and is therefore optional.

Token resolution order for :func:`query`:

1. Explicit ``token`` argument on :func:`query`.
2. The contextvar set by :func:`api_key_scope`.
3. ``os.environ["SANITY_API_TOKEN"]`` — **only when ``allow_env_token=True``**.

The env fallback is opt-in (``allow_env_token`` defaults to ``False``) so a
public-dataset read is reproducible and never silently authenticated by an
unrelated ambient token. If none resolve, the request is sent unauthenticated
(fine for public datasets).
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import requests

from .errors import SanityQueryError

# dlthub blog defaults — discovered from cdn.sanity.io image URLs on dlthub.com/blog
# and confirmed against the live (public) Query API.
DEFAULT_PROJECT_ID = "nsq559ov"
DEFAULT_DATASET = "production"
DEFAULT_API_VERSION = "v2025-02-19"

# Seconds before a single query request is abandoned.
_REQUEST_TIMEOUT = 30


@dataclass(frozen=True)
class SanityConfig:
    """Connection target for a Sanity dataset.

    ``use_cdn`` selects the cached edge host (``apicdn.sanity.io``). It defaults
    to ``False`` so every caller — not just the downloader's CLI — reads from
    the live origin (``api.sanity.io``) and never silently archives stale or
    just-deleted content; set it ``True`` to opt into the cached edge when
    latency matters more than freshness.
    """

    project_id: str = DEFAULT_PROJECT_ID
    dataset: str = DEFAULT_DATASET
    api_version: str = DEFAULT_API_VERSION
    use_cdn: bool = False

    def query_url(self) -> str:
        host = "apicdn" if self.use_cdn else "api"
        return (
            f"https://{self.project_id}.{host}.sanity.io"
            f"/{self.api_version}/data/query/{self.dataset}"
        )


_token_var: ContextVar[str | None] = ContextVar("sanity_api_token", default=None)


@contextmanager
def api_key_scope(token: str) -> Generator[None, None, None]:
    """Bind ``token`` as the active Sanity bearer token for this context.

    Mirrors :func:`libs.exa.client.api_key_scope`. Reset on exit so concurrent
    contexts do not leak tokens into one another.
    """
    handle = _token_var.set(token)
    try:
        yield
    finally:
        _token_var.reset(handle)


def _resolve_token(token: str | None, *, allow_env_token: bool) -> str | None:
    resolved = token or _token_var.get()
    if not resolved and allow_env_token:
        resolved = os.environ.get("SANITY_API_TOKEN", "")
    return (resolved or "").strip() or None


def query(
    groq: str,
    *,
    config: SanityConfig,
    params: dict[str, Any] | None = None,
    token: str | None = None,
    allow_env_token: bool = False,
) -> Any:
    """Run a GROQ query and return the ``result`` field.

    Args:
        groq: The GROQ query string.
        config: Target project/dataset/version.
        params: Optional GROQ query parameters. Each key ``k`` is sent as the
            URL param ``$k`` (Sanity's convention) JSON-encoded.
        token: Optional explicit bearer token (see module docstring for the
            full resolution order).
        allow_env_token: Defaults to ``False`` so the ``SANITY_API_TOKEN``
            environment fallback is ignored (an explicit ``token`` or the
            ``api_key_scope`` contextvar still apply). Opt in with ``True`` to
            let the env token authenticate the request.

    Raises:
        SanityQueryError: On a non-2xx response or a transport failure.
    """
    import json

    request_params: dict[str, str] = {"query": groq}
    for key, value in (params or {}).items():
        request_params[f"${key}"] = json.dumps(value)

    headers: dict[str, str] = {}
    resolved_token = _resolve_token(token, allow_env_token=allow_env_token)
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"

    try:
        response = requests.get(
            config.query_url(),
            params=request_params,
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SanityQueryError(f"Sanity query request failed: {exc}") from exc

    if not response.ok:
        raise SanityQueryError(
            f"Sanity query returned HTTP {response.status_code}: {response.text[:500]}",
            status=response.status_code,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise SanityQueryError(
            f"Sanity query returned a non-JSON response: {response.text[:500]}",
        ) from exc

    if not isinstance(payload, dict) or "result" not in payload:
        raise SanityQueryError(
            f"Sanity query response missing 'result' field: {str(payload)[:500]}",
        )
    return payload["result"]
