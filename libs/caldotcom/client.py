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
from collections.abc import Generator
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


class CalcomClient:
    """Thin synchronous client for the Cal.com v2 API."""

    def __init__(self, api_key: str, *, timeout: float = 10.0) -> None:
        # Cal.com v2 personal API keys (``cal_live_...``) authenticate via the
        # ``?apiKey=`` query parameter. ``Authorization: Bearer`` is only valid
        # for managed-user / OAuth tokens — sending a personal key as a Bearer
        # token returns 401 ``CustomThrottlerGuard - Invalid API Key``.
        self._client = httpx.Client(
            base_url=CALCOM_API_BASE,
            params={"apiKey": api_key},
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
        response = self._client.get(f"/v2/bookings/{booking_uid}")
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
