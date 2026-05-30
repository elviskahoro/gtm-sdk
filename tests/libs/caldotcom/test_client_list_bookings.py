"""Unit tests for ``CalcomClient.list_bookings`` using ``httpx.MockTransport``.

No network. Mirrors ``tests/libs/caldotcom/test_client.py``. Exercises the
``take``/``skip`` pagination contract (stop on a short page), the comma-joined
``status`` lifecycle query param, and per-row ``BookingCreatedPayload``
validation.
"""

from __future__ import annotations

import httpx
import pytest

from libs.caldotcom.client import CALCOM_API_BASE, CALCOM_API_VERSION, CalcomClient
from libs.caldotcom.models import BookingCreatedPayload


def _no_sleep(_seconds: float) -> None:
    """Typed stand-in for time.sleep so retry tests don't actually wait."""


def _booking(uid: str) -> dict[str, object]:
    return {
        "id": 1,
        "uid": uid,
        "title": "Discovery call",
        "status": "accepted",
        "start": "2026-06-01T15:00:00.000Z",
        "end": "2026-06-01T15:30:00.000Z",
        "hosts": [
            {
                "id": 1,
                "name": "Host",
                "email": "host@dlthub.com",
                "displayEmail": "host@dlthub.com",
                "username": "host",
                "timeZone": "UTC",
            },
        ],
        "attendees": [
            {
                "name": "Ext",
                "email": "ext@example.com",
                "displayEmail": "ext@example.com",
                "timeZone": "UTC",
                "absent": False,
            },
        ],
        "bookingFieldsResponses": {},
    }


def _client_with_handler(handler: httpx.MockTransport) -> CalcomClient:
    c = CalcomClient(api_key="cal_fake_token")
    # Mirror the production client: no baked-in apiKey param — auth is attached
    # per request (list_bookings → Bearer header, not the query param).
    c._client = httpx.Client(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        base_url=CALCOM_API_BASE,
        headers={
            "cal-api-version": CALCOM_API_VERSION,
            "Accept": "application/json",
        },
        transport=handler,
        timeout=10.0,
    )
    return c


def test_list_bookings_paginates_take_skip_until_short_page() -> None:
    """Three full pages of 2 + a final short page → 3 requests, all rows returned."""
    seen_skips: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/bookings"
        # The list endpoint requires Bearer auth, and must NOT leak the
        # ?apiKey= query param (which it rejects).
        assert request.headers["Authorization"] == "Bearer cal_fake_token"
        assert "apiKey" not in request.url.params
        skip = int(request.url.params["skip"])
        take = int(request.url.params["take"])
        assert take == 2
        seen_skips.append(skip)
        # 3 rows total across page size 2 → page A (2 rows, full), page B (1 row, short).
        all_uids = ["a", "b", "c"]
        page = all_uids[skip : skip + take]
        return httpx.Response(
            200,
            json={"status": "success", "data": [_booking(u) for u in page]},
        )

    c = _client_with_handler(httpx.MockTransport(respond))
    result = c.list_bookings(page_size=2)

    assert [b.uid for b in result] == ["a", "b", "c"]
    assert all(isinstance(b, BookingCreatedPayload) for b in result)
    # Stops once a page returns fewer than `take`: skip=0 (2 rows) then skip=2 (1 row).
    assert seen_skips == [0, 2]


def test_list_bookings_uses_pagination_has_next_page_cursor() -> None:
    """With the 2024-08-13 shape, stop on ``hasNextPage`` even on a full page."""
    seen_skips: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params["skip"])
        seen_skips.append(skip)
        # Two full pages of 2, but the server says page 2 is the last one.
        has_next = skip == 0
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": [_booking(f"u{skip}a"), _booking(f"u{skip}b")],
                "pagination": {"hasNextPage": has_next},
            },
        )

    c = _client_with_handler(httpx.MockTransport(respond))
    result = c.list_bookings(page_size=2)

    # Full second page would NOT stop under the short-page heuristic; the
    # hasNextPage cursor is what halts it.
    assert len(result) == 4
    assert seen_skips == [0, 2]


def test_list_bookings_sends_comma_joined_lifecycle_status() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["status"] == "past,upcoming"
        return httpx.Response(200, json={"status": "success", "data": []})

    c = _client_with_handler(httpx.MockTransport(respond))
    assert c.list_bookings(lifecycle_status=["past", "upcoming"]) == []


def test_list_bookings_wraps_bare_string_lifecycle() -> None:
    """A bare str must not be iterated char-by-char into 'p,a,s,t'."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["status"] == "past"
        return httpx.Response(200, json={"status": "success", "data": []})

    c = _client_with_handler(httpx.MockTransport(respond))
    assert c.list_bookings(lifecycle_status="past") == []


def test_list_bookings_forwards_time_filters() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["afterStart"] == "2026-01-01T00:00:00.000Z"
        assert request.url.params["beforeEnd"] == "2026-02-01T00:00:00.000Z"
        return httpx.Response(200, json={"status": "success", "data": []})

    c = _client_with_handler(httpx.MockTransport(respond))
    c.list_bookings(
        after_start="2026-01-01T00:00:00.000Z",
        before_end="2026-02-01T00:00:00.000Z",
    )


def test_list_bookings_rejects_non_positive_page_size() -> None:
    """A page_size of 0/negative would stall or reverse pagination — reject it."""

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"status": "success", "data": []})

    c = _client_with_handler(httpx.MockTransport(respond))
    for bad in (0, -1):
        with pytest.raises(ValueError, match="page_size must be >= 1"):
            c.list_bookings(page_size=bad)


def test_list_bookings_retries_transient_5xx_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx mid-fetch is retried (with no real sleep) rather than aborting."""
    import libs.caldotcom.client as client_mod

    monkeypatch.setattr(client_mod.time, "sleep", _no_sleep)
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"status": "success", "data": [_booking("a")]})

    c = _client_with_handler(httpx.MockTransport(respond))
    result = c.list_bookings(page_size=10)
    assert [b.uid for b in result] == ["a"]
    assert calls["n"] == 2  # retried past the 503


def test_list_bookings_raises_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import libs.caldotcom.client as client_mod

    monkeypatch.setattr(client_mod.time, "sleep", _no_sleep)

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(500, json={"error": "boom"})

    c = _client_with_handler(httpx.MockTransport(respond))
    with pytest.raises(httpx.HTTPStatusError):
        c.list_bookings(page_size=10)


def test_list_bookings_empty_first_page_stops_immediately() -> None:
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        return httpx.Response(200, json={"status": "success", "data": []})

    c = _client_with_handler(httpx.MockTransport(respond))
    assert c.list_bookings(page_size=50) == []
    assert calls["n"] == 1
