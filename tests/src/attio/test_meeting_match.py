"""Unit tests for the participant/start-window meeting matcher (ai-4bz).

The matcher lets a producer without a calendar ``ical_uid`` (Fathom) resolve the
existing calendar-synced Attio meeting instead of creating a duplicate. Attio is
mocked via ``list_candidate_meetings`` so these stay offline.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from libs.attio.models import MeetingCandidate
from src.attio import meeting_match

START = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)


def _candidate(
    meeting_id: str,
    *,
    minute_offset: int,
    emails: list[str],
    created_by_system: bool = False,
) -> MeetingCandidate:
    return MeetingCandidate(
        meeting_id=meeting_id,
        title=f"meeting {meeting_id}",
        start=START.replace(minute=START.minute + minute_offset),
        participant_emails=sorted(e.lower() for e in emails),
        created_by_system=created_by_system,
    )


@pytest.fixture
def patch_candidates(monkeypatch):
    def _set(candidates: list[MeetingCandidate]) -> None:
        def _fake(**_: object) -> list[MeetingCandidate]:
            return list(candidates)

        monkeypatch.setattr(meeting_match, "list_candidate_meetings", _fake)

    return _set


def test_no_candidates_returns_none(patch_candidates):
    patch_candidates([])
    assert (
        meeting_match.resolve_meeting_id_by_participants(
            start=START,
            participant_emails=["a@dlthub.com", "b@acme.com"],
        )
        is None
    )


def test_single_overlapping_candidate_matches(patch_candidates):
    patch_candidates(
        [_candidate("m1", minute_offset=0, emails=["a@dlthub.com", "b@acme.com"])],
    )
    assert (
        meeting_match.resolve_meeting_id_by_participants(
            start=START,
            participant_emails=["a@dlthub.com", "b@acme.com"],
        )
        == "m1"
    )


def test_subset_participants_still_match_via_overlap_coefficient(patch_candidates):
    # Calendar meeting has MORE attendees than Fathom captured — overlap
    # coefficient is 1.0 (Fathom set ⊆ calendar set), so it still matches.
    patch_candidates(
        [
            _candidate(
                "m1",
                minute_offset=0,
                emails=["a@dlthub.com", "b@acme.com", "c@acme.com"],
            ),
        ],
    )
    assert (
        meeting_match.resolve_meeting_id_by_participants(
            start=START,
            participant_emails=["a@dlthub.com"],
        )
        == "m1"
    )


def test_non_overlapping_candidate_is_not_matched(patch_candidates):
    # A different meeting in the same window with no shared participants must not
    # be matched — better to create a fresh meeting than mis-attach.
    patch_candidates(
        [_candidate("m1", minute_offset=2, emails=["x@other.com", "y@other.com"])],
    )
    assert (
        meeting_match.resolve_meeting_id_by_participants(
            start=START,
            participant_emails=["a@dlthub.com", "b@acme.com"],
        )
        is None
    )


def test_clear_winner_by_overlap_then_proximity(patch_candidates):
    patch_candidates(
        [
            _candidate("far", minute_offset=8, emails=["a@dlthub.com", "b@acme.com"]),
            _candidate("near", minute_offset=0, emails=["a@dlthub.com", "b@acme.com"]),
        ],
    )
    assert (
        meeting_match.resolve_meeting_id_by_participants(
            start=START,
            participant_emails=["a@dlthub.com", "b@acme.com"],
        )
        == "near"
    )


def test_tie_without_llm_falls_back_deterministically(patch_candidates):
    # Two indistinguishable candidates (same overlap, same proximity): with the
    # LLM disabled the matcher returns a stable deterministic pick (min id).
    patch_candidates(
        [
            _candidate("m2", minute_offset=0, emails=["a@dlthub.com", "b@acme.com"]),
            _candidate("m1", minute_offset=0, emails=["a@dlthub.com", "b@acme.com"]),
        ],
    )
    assert (
        meeting_match.resolve_meeting_id_by_participants(
            start=START,
            participant_emails=["a@dlthub.com", "b@acme.com"],
            use_llm_tiebreak=False,
        )
        == "m1"
    )


def test_prefers_calendar_synced_over_api_duplicate(patch_candidates):
    # Same slot, same participants: a calendar-synced (system) meeting and an
    # api-token duplicate a prior run minted. The matcher must pick the system
    # one so the recording attaches to the canonical calendar meeting (ai-4bz).
    patch_candidates(
        [
            _candidate(
                "api-dup",
                minute_offset=0,
                emails=["a@dlthub.com", "b@acme.com"],
                created_by_system=False,
            ),
            _candidate(
                "calendar",
                minute_offset=0,
                emails=["a@dlthub.com", "b@acme.com"],
                created_by_system=True,
            ),
        ],
    )
    assert (
        meeting_match.resolve_meeting_id_by_participants(
            start=START,
            participant_emails=["a@dlthub.com", "b@acme.com"],
            use_llm_tiebreak=False,
        )
        == "calendar"
    )


def test_empty_participant_emails_returns_none(patch_candidates):
    patch_candidates(
        [_candidate("m1", minute_offset=0, emails=["a@dlthub.com"])],
    )
    assert (
        meeting_match.resolve_meeting_id_by_participants(
            start=START,
            participant_emails=[],
        )
        is None
    )
