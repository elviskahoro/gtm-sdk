"""Cal.com REST API v2 client.

Only ``GET /v2/bookings/{uid}`` is needed today — ``BOOKING_NO_SHOW_UPDATED``
webhooks are too slim (just ``bookingUid`` + per-attendee ``noShow`` flags) to
compute ``canonical_meeting_uid`` directly, so the handler must fetch the full
booking to learn host email + start time. All other Cal.com triggers carry
enough information in-payload.

Errors propagate (unlike ``libs/harvest/client.py`` which swallows them) so the
NO_SHOW op surfaces a failed ``ReliabilityEnvelope`` if Cal.com is unreachable —
silent failure here would hide the gap and corrupt audit logs.
"""

from __future__ import annotations

import os
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from types import TracebackType
from typing import Any

import httpx

from libs.caldotcom.models import BookingCreatedPayload

CALCOM_API_BASE = "https://api.cal.com"

_api_key_var: ContextVar[str | None] = ContextVar(
    "calcom_api_key",
    default=None,
)


@contextmanager
def api_key_scope(api_key: str) -> Generator[None, None, None]:
    """Bind ``api_key`` as the active Cal.com key for this async/sync context.

    Mirrors :func:`libs.attio.client.api_key_scope` — the webhook flow opens
    this scope after fetching ``CALCOM_API_KEY`` from Infisical, and
    :meth:`CalcomClient.from_env` reads from the contextvar before falling
    back to ``os.environ``. The scope is reset on exit so concurrent
    requests in the same Modal container do not see each other's keys.
    """
    token = _api_key_var.set(api_key)
    try:
        yield
    finally:
        _api_key_var.reset(token)


# Pinned per Cal.com docs: "BookingOutput_2024_08_13" shape matches our
# BookingCreatedPayload model. Bump deliberately when migrating.
CALCOM_API_VERSION = "2024-08-13"

# list_bookings walks many pages for a bulk backfill; one transient blip
# shouldn't abort the whole fetch. Retry 429/5xx/network with bounded backoff
# (honoring Retry-After on 429); deterministic 4xx falls through to the caller's
# raise_for_status.
_LIST_MAX_ATTEMPTS = 4
_LIST_BACKOFF_BASE_SECONDS = 1.0


class CalcomClient:
    """Thin synchronous client for the Cal.com v2 API."""

    def __init__(self, api_key: str, *, timeout: float = 10.0) -> None:
        # Cal.com's v2 auth is endpoint-inconsistent (confirmed against the live
        # API 2026-05-29):
        #   * Single-booking ``GET /v2/bookings/{uid}`` authenticates via the
        #     ``?apiKey=`` query parameter; sending the personal key as a Bearer
        #     token there returns 401 ``CustomThrottlerGuard - Invalid API Key``.
        #   * The collection ``GET /v2/bookings`` (see ``list_bookings``) does the
        #     OPPOSITE: it ignores ``?apiKey=`` (→ 403 "no authentication
        #     provided … bearer token / oAuth client id") and requires the same
        #     personal key sent as ``Authorization: Bearer``.
        # Auth is attached per-request, NOT baked into the client, so the two
        # forms never bleed into each other: ``get_booking`` passes the
        # ``?apiKey=`` query param, ``list_bookings`` sends ``Authorization:
        # Bearer``. (An earlier version baked ``params={"apiKey": ...}`` into the
        # client; the collection endpoint then received both the Bearer header
        # AND the query param it rejects, which only worked because Cal.com
        # happened to ignore the stray param — fragile, so we stopped relying on
        # it.)
        self._api_key = api_key
        self._client = httpx.Client(
            base_url=CALCOM_API_BASE,
            headers={
                "cal-api-version": CALCOM_API_VERSION,
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def get_booking(self, booking_uid: str) -> BookingCreatedPayload | None:
        """Fetch the full booking by uid.

        Returns ``None`` only on 404 (booking deleted or not found). All other
        non-success codes raise ``httpx.HTTPStatusError`` — the NO_SHOW handler
        turns those into a failed ReliabilityEnvelope so the webhook caller
        sees the gap.

        Cal.com wraps the success body as ``{"status": "success", "data": {...}}``
        with ``data`` matching ``BookingOutput_2024_08_13`` (= our
        ``BookingCreatedPayload``). The wrapper key is also accepted as a flat
        body for forward compatibility.
        """
        response = self._client.get(
            f"/v2/bookings/{booking_uid}",
            params={"apiKey": self._api_key},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()

        body: Any = response.json()
        data = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(data, dict):
            raise httpx.HTTPError(
                f"Cal.com /v2/bookings/{booking_uid} returned non-dict body",
            )
        # Inject ``triggerEvent`` so the discriminated union resolves.
        return BookingCreatedPayload.model_validate(
            {"triggerEvent": "BOOKING_CREATED", **data},
        )

    def list_bookings(
        self,
        *,
        lifecycle_status: Sequence[str] | None = None,
        after_start: str | None = None,
        before_end: str | None = None,
        page_size: int = 100,
    ) -> list[BookingCreatedPayload]:
        """List bookings, paginating the collection endpoint to exhaustion.

        Used by the historical backfill (``scripts/caldotcom-backfill-bookings.py``)
        to replay every past booking through the live Modal webhook. The
        single-booking ``get_booking`` only covers the NO_SHOW handler's needs;
        this walks ``GET /v2/bookings``.

        Pagination is ``take`` (page size) / ``skip`` (offset); a page returning
        fewer than ``take`` rows is the last one. Each row is validated as
        :class:`BookingCreatedPayload` with the same ``triggerEvent`` injection
        and ``{"data": [...]}`` unwrapping as :meth:`get_booking`. Errors
        propagate (module contract — a partial backfill must fail loudly, not
        silently truncate).

        ``lifecycle_status`` maps to the collection endpoint's ``status`` query
        param, whose vocabulary is a booking *lifecycle* bucket
        (``upcoming``/``recurring``/``past``/``cancelled``/``unconfirmed``) and is
        serialized comma-joined per the Cal.com spec (``?status=upcoming,past``).
        This is NOT the per-booking RSVP ``status`` field
        (``accepted``/``pending``/``cancelled``/``rejected``) the webhook maps to
        an Attio RSVP value — the backfill filters on that record field
        separately. Pass ``None`` to accept the API default.

        Auth: unlike ``get_booking``, this endpoint rejects the ``?apiKey=``
        query param (403) and requires the personal key as ``Authorization:
        Bearer`` — see the ``__init__`` note. The ``cal-api-version: 2024-08-13``
        header (baked into the client) is also required here: older versions
        return a nested ``data.bookings`` shape instead of the flat ``data: []``
        list this method expects.
        """
        if page_size < 1:
            raise ValueError(
                f"page_size must be >= 1, got {page_size}: a non-positive page "
                "size would stall or reverse the take/skip pagination loop.",
            )
        # A bare str is itself a Sequence[str]; without this it would be joined
        # character-by-character ("past" → "p,a,s,t") into a bogus query param.
        if isinstance(lifecycle_status, str):
            lifecycle_status = [lifecycle_status]
        bookings: list[BookingCreatedPayload] = []
        auth_header = {"Authorization": f"Bearer {self._api_key}"}
        skip = 0
        while True:
            params: dict[str, Any] = {"take": page_size, "skip": skip}
            if lifecycle_status:
                params["status"] = ",".join(lifecycle_status)
            if after_start:
                params["afterStart"] = after_start
            if before_end:
                params["beforeEnd"] = before_end

            response = self._get_bookings_page(params, auth_header)
            response.raise_for_status()

            body: Any = response.json()
            data = body.get("data", body) if isinstance(body, dict) else body
            if not isinstance(data, list):
                raise httpx.HTTPError(
                    "Cal.com /v2/bookings returned a non-list 'data' body. "
                    "This usually means the cal-api-version header is missing or "
                    "older than 2024-08-13 (which nests bookings under data.bookings).",
                )

            for item in data:
                bookings.append(
                    BookingCreatedPayload.model_validate(
                        {"triggerEvent": "BOOKING_CREATED", **item},
                    ),
                )

            # Prefer the server's explicit pagination cursor when present
            # (2024-08-13 returns it); fall back to the short-page heuristic.
            pagination = body.get("pagination") if isinstance(body, dict) else None
            if isinstance(pagination, dict) and "hasNextPage" in pagination:
                if not pagination["hasNextPage"]:
                    break
            elif len(data) < page_size:
                break
            skip += page_size

        return bookings

    def _get_bookings_page(
        self,
        params: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        """GET one /v2/bookings page, retrying only transient failures.

        Returns the response on success OR a deterministic 4xx (the caller's
        ``raise_for_status`` surfaces the latter). Retries 429 (honoring
        ``Retry-After``), 5xx, and network/timeout errors with bounded backoff;
        raises the last error once attempts are exhausted so a partial backfill
        fails loudly rather than truncating silently.
        """
        last_error: Exception | None = None
        for attempt in range(1, _LIST_MAX_ATTEMPTS + 1):
            retry_after: float | None = None
            try:
                response = self._client.get(
                    "/v2/bookings",
                    params=params,
                    headers=headers,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
            else:
                code = response.status_code
                if code != 429 and code < 500:
                    # Success or deterministic 4xx — let the caller handle it.
                    return response
                last_error = httpx.HTTPStatusError(
                    f"transient {code} from /v2/bookings",
                    request=response.request,
                    response=response,
                )
                if code == 429:
                    raw = response.headers.get("Retry-After")
                    if raw is not None:
                        try:
                            secs = float(raw)
                        except ValueError:
                            secs = -1.0
                        retry_after = secs if secs >= 0 else None
            if attempt < _LIST_MAX_ATTEMPTS:
                time.sleep(
                    retry_after
                    if retry_after is not None
                    else _LIST_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
                )
        assert (
            last_error is not None
        )  # loop runs >= 1 time and only exits here on error
        raise last_error

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CalcomClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @classmethod
    def from_env(cls) -> CalcomClient:
        """Build a client from the active key for this request.

        Resolution order: the contextvar set by :func:`api_key_scope` first
        (webhook flow — populated from Infisical at request boundary), then
        ``CALCOM_API_KEY`` in ``os.environ`` (back-compat for any local
        invocation outside the scope). The webhook runtime no longer binds
        a named ``caldotcom`` Modal Secret; the key lives in Infisical.
        """
        api_key = (_api_key_var.get() or os.environ.get("CALCOM_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError(
                "CALCOM_API_KEY not resolved. Provide one of: "
                "(1) call inside libs.caldotcom.client.api_key_scope(...), "
                "(2) set CALCOM_API_KEY in the process environment. "
                "Required for BOOKING_NO_SHOW_UPDATED handling.",
            )
        return cls(api_key=api_key)
