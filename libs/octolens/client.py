"""Octolens v2 REST API client.

The rest of this package models *inbound* webhook payloads
(:class:`libs.octolens.models.Webhook`); this is the only *outbound* caller. It
pulls historical mentions from ``POST /api/v2/mentions`` for the dlt/dlthub
backfill (``scripts/octolens-mentions-backfill.py --source api``).

Auth is a Bearer API key (Octolens Settings → API, ``read`` scope) provided as
``OCTOLENS_API_KEY``. The v2 API is rate-limited to **500 requests/hour/org**;
:meth:`OctolensClient.list_mentions` paginates with the opaque cursor and paces
itself off the ``X-RateLimit-Remaining``/``X-RateLimit-Reset`` headers
(sleeping until the window resets when the budget is exhausted), falling back to
honoring ``Retry-After`` on a reactive 429. Transient 5xx / network blips retry
with bounded backoff; deterministic auth/validation 4xx raise
:class:`OctolensApiError`. Errors propagate — a bulk backfill must fail loudly
rather than silently truncate (same contract as ``libs/caldotcom/client.py``).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from types import TracebackType
from typing import Any

import httpx
from pydantic import ValidationError

from libs.octolens.models import ApiMention

logger = logging.getLogger(__name__)

OCTOLENS_API_BASE = "https://app.octolens.com"
_MENTIONS_PATH = "/api/v2/mentions"
_KEYWORDS_PATH = "/api/v2/keywords"

# Page walk for a bulk backfill: one transient blip shouldn't abort the whole
# pull. Retry 429/5xx/network with bounded backoff; deterministic auth/validation
# 4xx fails loudly.
_MAX_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 1.0
# Transient 4xx worth retrying (alongside all 5xx): 408 Request Timeout and 429
# Rate Limited. Every other 4xx (auth/validation/not-found) is deterministic.
_RETRYABLE_4XX = frozenset({408, 429})
# Hard ceiling on a proactive rate-limit sleep so a bogus Reset header can't
# wedge the run for hours.
_MAX_RATELIMIT_SLEEP_SECONDS = 3600.0


class OctolensApiError(RuntimeError):
    """A terminal (non-retryable) Octolens API error — auth, validation, etc."""


class OctolensClient:
    """Thin synchronous client for the Octolens v2 API."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OctolensClient requires a non-empty api_key")
        # `transport` is injected only by tests (httpx.MockTransport); production
        # callers leave it None and get the default networking transport.
        self._client = httpx.Client(
            base_url=OCTOLENS_API_BASE,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_env(cls, *, timeout: float = 30.0) -> OctolensClient:
        """Build a client from ``OCTOLENS_API_KEY`` in the process environment.

        Raises ``RuntimeError`` (not :class:`OctolensApiError`) when unset, so
        the operator script can attach its Infisical-injection hint without
        coupling this adapter to ``scripts/``.
        """
        api_key = (os.environ.get("OCTOLENS_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError(
                "OCTOLENS_API_KEY not set. Mint a read-scope key in Octolens "
                "Settings → API and inject it via Infisical before running.",
            )
        return cls(api_key, timeout=timeout)

    def list_keywords(self) -> list[dict[str, Any]]:
        """Return every keyword tracked by the org (``GET /api/v2/keywords``).

        Each dict carries ``id`` (stable numeric keyword id) + ``keyword`` (the
        tracked phrase) among other fields. The backfill uses this to resolve
        ``dlt``/``dlthub`` to the numeric ids the mentions ``keyword`` filter
        requires, rather than hard-coding ids that differ per org.

        Unlike ``/api/v2/mentions``, this endpoint is **not paginated** — the v2
        spec defines no ``limit``/``cursor`` params and returns the org's full
        keyword set in one ``data`` array (orgs track tens of keywords, not
        thousands). A caller that wants to skip resolution entirely can pass
        numeric ids straight to ``list_mentions(filters={"keyword": [...]})``.
        """
        response = self._request_with_retry(lambda: self._client.get(_KEYWORDS_PATH))
        payload: Any = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise OctolensApiError(
                "Octolens /api/v2/keywords returned a non-list 'data' field",
            )
        return data

    def list_mentions(
        self,
        *,
        filters: dict[str, Any] | None = None,
        view: int | None = None,
        include_all: bool = False,
        page_size: int = 100,
        max_pages: int | None = None,
    ) -> Iterator[ApiMention]:
        """Yield every mention matching ``filters``, paginating to exhaustion.

        ``filters`` is the v2 ``ApiFilters`` object (simple or advanced); pass
        ``None`` to pull the whole organization (the backfill then narrows
        client-side via ``src.octolens.backfill.include_mention``). ``include_all``
        maps to the API's ``includeAll`` — ``False`` (default) returns only
        high+medium relevance (scores 0/1); ``True`` also includes low (score 2,
        which the Attio webhook drops anyway). ``max_pages`` is a safety cap for
        exploratory runs; ``None`` walks until the cursor is exhausted.
        """
        if not 1 <= page_size <= 100:
            raise ValueError(
                f"page_size must be 1..100 (the API cap), got {page_size}",
            )
        cursor: str | None = None
        pages = 0
        while True:
            body: dict[str, Any] = {"limit": page_size, "includeAll": include_all}
            if filters is not None:
                body["filters"] = filters
            if view is not None:
                body["view"] = view
            if cursor is not None:
                body["cursor"] = cursor

            response = self._request_with_retry(
                lambda body=body: self._client.post(_MENTIONS_PATH, json=body),
            )
            payload: Any = response.json()
            if not isinstance(payload, dict):
                raise OctolensApiError(
                    "Octolens /api/v2/mentions returned a non-object body",
                )
            data = payload.get("data", [])
            if not isinstance(data, list):
                raise OctolensApiError(
                    "Octolens /api/v2/mentions returned a non-list 'data' field",
                )
            for item in data:
                # Per-item: a single shape-shifted record must not abort a
                # multi-page bulk pull (the ApiMention contract). The model is
                # already lenient (all-optional + extra="allow"), so this only
                # trips on a type mismatch on a modeled field; log + skip it.
                try:
                    yield ApiMention.model_validate(item)
                except ValidationError as exc:
                    ident = (
                        item.get("sourceId") or item.get("url")
                        if isinstance(item, dict)
                        else None
                    )
                    logger.warning(
                        "skipping unparseable Octolens mention %s: %s",
                        ident,
                        exc,
                    )

            pages += 1
            pagination = payload.get("pagination")
            cursor = (
                pagination.get("nextCursor") if isinstance(pagination, dict) else None
            )
            if not cursor:
                break
            if max_pages is not None and pages >= max_pages:
                break
            self._respect_rate_limit(response)

    def _request_with_retry(
        self,
        send: Callable[[], httpx.Response],
    ) -> httpx.Response:
        """Issue one request via ``send``, retrying only transient failures.

        Returns the response on a 2xx. Raises :class:`OctolensApiError` on a
        deterministic 4xx (auth/validation — retrying won't help). Retries 429
        (honoring ``Retry-After``), 5xx, and network/timeout errors with bounded
        backoff; re-raises the last error once attempts are exhausted.
        """
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            retry_after: float | None = None
            try:
                response = send()
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
            else:
                code = response.status_code
                if code < 400:
                    return response
                if code < 500 and code not in _RETRYABLE_4XX:
                    # Deterministic 4xx — surface the stable error envelope.
                    raise OctolensApiError(self._error_detail(response))
                last_error = httpx.HTTPStatusError(
                    f"transient {code} from {response.request.url.path}",
                    request=response.request,
                    response=response,
                )
                if code == 429:
                    retry_after = _parse_retry_after(response)
            if attempt < _MAX_ATTEMPTS:
                time.sleep(
                    retry_after
                    if retry_after is not None
                    else _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
                )
        assert last_error is not None  # loop runs >= 1 time, only exits here on error
        raise last_error

    @staticmethod
    def _respect_rate_limit(response: httpx.Response) -> None:
        """Sleep until the window resets when the request budget is exhausted.

        Proactive throttle so a long backfill glides under the 500/hr cap instead
        of repeatedly tripping a reactive 429. No-op when the headers are absent
        or the budget still has room.
        """
        remaining_raw = response.headers.get("X-RateLimit-Remaining")
        if remaining_raw is None:
            return
        try:
            remaining = int(remaining_raw)
        except ValueError:
            return
        if remaining > 0:
            return
        try:
            reset_ts = float(response.headers.get("X-RateLimit-Reset", "0"))
        except ValueError:
            reset_ts = 0.0
        sleep_s = max(0.0, min(reset_ts - time.time(), _MAX_RATELIMIT_SLEEP_SECONDS))
        if sleep_s > 0:
            time.sleep(sleep_s)

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """Format the stable ``ErrorResponse`` envelope for a terminal 4xx."""
        try:
            payload = response.json()
        except (ValueError, httpx.DecodingError):
            payload = None
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                code = err.get("code")
                message = err.get("message")
                if code or message:
                    return f"Octolens API {response.status_code} {code}: {message}"
        return f"Octolens API {response.status_code}: {response.text[:200]}"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OctolensClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse a numeric ``Retry-After`` (seconds); ``None`` when absent/invalid.

    Clamped to ``_MAX_RATELIMIT_SLEEP_SECONDS`` so an oversized or bogus header
    can't stall the backfill for hours — beyond the clamp we'd rather fall back
    to bounded exponential backoff than honor an absurd wait.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        secs = float(raw)
    except ValueError:
        return None
    if secs < 0:
        return None
    return min(secs, _MAX_RATELIMIT_SLEEP_SECONDS)
