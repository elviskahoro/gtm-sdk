from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from attio.errors.sdkerror import SDKError

from libs.attio.models import (
    MeetingExternalRef,
    MeetingInput,
    MeetingParticipantInput,
)


def _make_sdk_error(status_code: int, message: str = "boom") -> SDKError:
    """Build a SDKError without spinning up a real httpx.Response.

    Mirrors the helper in test_attributes.py — SDKError.__init__ only reads
    .status_code/.headers/.text, so a SimpleNamespace duck-types fine.
    """
    raw_response = SimpleNamespace(status_code=status_code, headers={}, text=message)
    return SDKError(message, raw_response, message)  # type: ignore[arg-type]


def _input(**overrides: Any) -> MeetingInput:
    base: dict[str, Any] = dict(
        external_ref=MeetingExternalRef(
            ical_uid="fathom-call-1",
            provider="google",
            is_recurring=False,
        ),
        title="t",
        description="d",
        start=datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc),
        is_all_day=False,
        participants=[
            MeetingParticipantInput(email_address="a@example.com", is_organizer=True),
        ],
    )
    base.update(overrides)
    return MeetingInput(**base)


def test_find_or_create_meeting_returns_success_envelope(monkeypatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    from libs.attio.meetings import find_or_create_meeting
    from libs.attio.models import MeetingResult

    fake_result = MeetingResult(
        meeting_id="11111111-1111-1111-1111-111111111111",
        workspace_id="22222222-2222-2222-2222-222222222222",
        title="t",
        external_ref_ical_uid="fathom-call-1",
        created=True,
    )
    with patch("libs.attio.meetings._post_meeting", return_value=fake_result):
        envelope = find_or_create_meeting(_input())

    assert envelope.success is True
    assert envelope.record_id == "11111111-1111-1111-1111-111111111111"
    assert envelope.action == "created"
    assert envelope.meta["external_ref_ical_uid"] == "fathom-call-1"


def test_find_or_create_meeting_auth_missing(monkeypatch) -> None:
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    from libs.attio.meetings import find_or_create_meeting

    envelope = find_or_create_meeting(_input())
    assert envelope.success is False
    assert envelope.action == "failed"
    assert envelope.errors
    assert envelope.errors[0].fatal is True


def test_find_or_create_meeting_classified_validation_error(monkeypatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    from libs.attio.errors import AttioValidationError
    from libs.attio.meetings import find_or_create_meeting

    with patch(
        "libs.attio.meetings._post_meeting",
        side_effect=AttioValidationError("invalid title"),
    ):
        envelope = find_or_create_meeting(_input())

    assert envelope.success is False
    assert envelope.action == "failed"
    assert envelope.errors[0].error_type == "AttioValidationError"
    assert "invalid title" in envelope.errors[0].message


def test_find_or_create_meeting_classified_not_found(monkeypatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    from libs.attio.errors import AttioNotFoundError
    from libs.attio.meetings import find_or_create_meeting

    with patch(
        "libs.attio.meetings._post_meeting",
        side_effect=AttioNotFoundError("meetings not provisioned in this workspace"),
    ):
        envelope = find_or_create_meeting(_input())

    assert envelope.success is False
    assert envelope.action == "failed"
    assert envelope.errors[0].code == "not_found"
    assert envelope.errors[0].error_type == "AttioNotFoundError"
    assert envelope.errors[0].fatal is True


def _client_raising(exc: Exception):
    """A get_client() stand-in whose post_v2_meetings raises ``exc``."""

    def _raise(**_kwargs: object) -> object:
        raise exc

    @contextmanager
    def _cm():
        yield SimpleNamespace(meetings=SimpleNamespace(post_v2_meetings=_raise))

    return _cm


def test_post_meeting_translates_sdk_404(monkeypatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    from libs.attio.errors import AttioNotFoundError
    from libs.attio.meetings import (
        _post_meeting,  # pyright: ignore[reportPrivateUsage]
    )

    with (
        patch(
            "libs.attio.meetings.get_client",
            _client_raising(_make_sdk_error(404, "no meetings")),
        ),
        pytest.raises(AttioNotFoundError),
    ):
        _post_meeting(_input())


def test_post_meeting_reraises_non_404_sdk_error(monkeypatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    from libs.attio.meetings import (
        _post_meeting,  # pyright: ignore[reportPrivateUsage]
    )

    with (
        patch(
            "libs.attio.meetings.get_client",
            _client_raising(_make_sdk_error(500, "server error")),
        ),
        pytest.raises(SDKError),
    ):
        _post_meeting(_input())


def _sdk_meeting(
    meeting_id: str,
    *,
    start_iso: str,
    emails: list[str],
    actor_type: str,
    created_at: str,
) -> SimpleNamespace:
    """A duck-typed stand-in for an SDK ``Meeting`` as ``get_v2_meetings`` returns
    it — only the attributes ``_candidate_from_meeting`` reads."""
    return SimpleNamespace(
        id=SimpleNamespace(meeting_id=meeting_id),
        title=f"m-{meeting_id}",
        start=SimpleNamespace(datetime_=start_iso, date_=None),
        participants=[SimpleNamespace(email_address=e) for e in emails],
        created_by_actor=SimpleNamespace(type=actor_type),
        created_at=created_at,
    )


def _paged_client(pages: list[list[SimpleNamespace]], calls: list[dict[str, object]]):
    """get_client() stand-in paginating ``pages`` via the cursor protocol.

    Records each ``get_v2_meetings`` kwargs dict into ``calls`` so a test can
    assert the server-side bounds/cursor handling.
    """

    def _get(**kwargs: object) -> SimpleNamespace:
        calls.append(dict(kwargs))
        cursor = kwargs.get("cursor")
        idx = 0 if cursor is None else int(cursor)  # type: ignore[arg-type]
        next_cursor = str(idx + 1) if idx + 1 < len(pages) else None
        return SimpleNamespace(
            data=pages[idx],
            pagination=SimpleNamespace(next_cursor=next_cursor),
        )

    @contextmanager
    def _cm(*_a: object, **_k: object):
        yield SimpleNamespace(meetings=SimpleNamespace(get_v2_meetings=_get))

    return _cm


def test_iter_meetings_in_range_paginates_and_maps(monkeypatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    from libs.attio.meetings import iter_meetings_in_range

    pages = [
        [
            _sdk_meeting(
                "m1",
                start_iso="2026-05-10T15:00:00Z",
                emails=["A@X.com", "b@x.com"],
                actor_type="system",
                created_at="2026-05-01T00:00:00Z",
            ),
        ],
        [
            _sdk_meeting(
                "m2",
                start_iso="2026-05-11T16:30:00+00:00",
                emails=["c@x.com"],
                actor_type="api-token",
                created_at="2026-06-09T12:00:00Z",
            ),
        ],
    ]
    calls: list[dict[str, object]] = []
    with patch("libs.attio.meetings.get_client", _paged_client(pages, calls)):
        out = list(
            iter_meetings_in_range(
                start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                end=datetime(2026, 5, 31, tzinfo=timezone.utc),
            ),
        )

    # Both pages consumed via the cursor protocol (2 calls: cursor=None, then "1").
    assert len(calls) == 2
    assert calls[0]["cursor"] is None
    assert calls[1]["cursor"] == "1"
    # Server-side bounds sent for both supplied bounds.
    assert "starts_before" in calls[0] and "ends_from" in calls[0]

    assert [m.meeting_id for m in out] == ["m1", "m2"]
    m1, m2 = out
    # Field mapping: actor type, created_by_system, lowercased+sorted emails.
    assert m1.created_by_type == "system"
    assert m1.created_by_system is True
    assert m1.participant_emails == ["a@x.com", "b@x.com"]
    assert m1.created_at == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert m2.created_by_type == "api-token"
    assert m2.created_by_system is False
    assert m2.created_at == datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)


def test_iter_meetings_in_range_trims_out_of_range_starts(monkeypatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    from libs.attio.meetings import iter_meetings_in_range

    pages = [
        [
            _sdk_meeting(
                "before",
                start_iso="2026-04-30T23:59:00Z",  # < start
                emails=["a@x.com"],
                actor_type="system",
                created_at="2026-04-01T00:00:00Z",
            ),
            _sdk_meeting(
                "inrange",
                start_iso="2026-05-15T12:00:00Z",
                emails=["a@x.com"],
                actor_type="system",
                created_at="2026-05-01T00:00:00Z",
            ),
            _sdk_meeting(
                "after",
                start_iso="2026-06-01T00:00:01Z",  # > end
                emails=["a@x.com"],
                actor_type="system",
                created_at="2026-05-01T00:00:00Z",
            ),
        ],
    ]
    calls: list[dict[str, object]] = []
    with patch("libs.attio.meetings.get_client", _paged_client(pages, calls)):
        out = list(
            iter_meetings_in_range(
                start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                end=datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc),
            ),
        )

    # The partial-page rows outside [start, end] are trimmed client-side.
    assert [m.meeting_id for m in out] == ["inrange"]


def test_iter_meetings_in_range_unbounded_sends_no_bounds(monkeypatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    from libs.attio.meetings import iter_meetings_in_range

    pages = [
        [
            _sdk_meeting(
                "m1",
                start_iso="2025-01-01T00:00:00Z",
                emails=["a@x.com"],
                actor_type="system",
                created_at="2025-01-01T00:00:00Z",
            ),
        ],
    ]
    calls: list[dict[str, object]] = []
    with patch("libs.attio.meetings.get_client", _paged_client(pages, calls)):
        out = list(iter_meetings_in_range())

    assert [m.meeting_id for m in out] == ["m1"]
    # No start/end → neither server-side bound is sent (defaults to UNSET).
    assert "starts_before" not in calls[0]
    assert "ends_from" not in calls[0]


def test_iter_meetings_in_range_clamps_page_limit_to_api_max(monkeypatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    from libs.attio.meetings import iter_meetings_in_range

    pages = [
        [
            _sdk_meeting(
                "m1",
                start_iso="2025-01-01T00:00:00Z",
                emails=["a@x.com"],
                actor_type="system",
                created_at="2025-01-01T00:00:00Z",
            ),
        ],
    ]
    calls: list[dict[str, object]] = []
    with patch("libs.attio.meetings.get_client", _paged_client(pages, calls)):
        list(iter_meetings_in_range(limit=10_000))

    # Oversized page size is clamped to Attio's 200-row cap, not forwarded as-is.
    assert calls[0]["limit"] == 200
