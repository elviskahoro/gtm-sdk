from __future__ import annotations

from libs.granola.models import NormalizedMeeting
from libs.granola.state import ExportState, compute_meeting_hash, should_write


def _meeting() -> NormalizedMeeting:
    return NormalizedMeeting(
        id="m1",
        title="Standup",
        notes_markdown="notes",
        transcript_segments=[],
        transcript_source="local",
        transcript_status="missing",
    )


def test_unchanged_hash_skips_write() -> None:
    meeting = _meeting()
    digest = compute_meeting_hash(meeting)
    state = ExportState(hashes={"m1": digest})
    assert should_write("m1", digest, state) is False


def test_changed_hash_writes() -> None:
    meeting = _meeting()
    digest = compute_meeting_hash(meeting)
    state = ExportState(hashes={"m1": "other"})
    assert should_write("m1", digest, state) is True
