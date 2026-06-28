"""Unit tests for synthetic-duplicate meeting detection (ai-4bz.9).

``detect_orphans`` takes an iterable of ``MeetingCandidate`` directly, so these
stay fully offline — no Attio/network calls.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import pytest

from libs.attio.models import MeetingCandidate
from src.attio.orphan_meetings import (
    CSV_FIELDNAMES,
    classify,
    detect_orphans,
    write_orphan_csvs,
)

START = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)


def _cand(
    meeting_id: str,
    *,
    minute_offset: int = 0,
    emails: list[str],
    actor: str,
) -> MeetingCandidate:
    return MeetingCandidate(
        meeting_id=meeting_id,
        title=f"meeting {meeting_id}",
        start=START.replace(minute=START.minute + minute_offset),
        participant_emails=sorted(e.lower() for e in emails),
        created_by_system=actor == "system",
        created_by_type=actor,
        created_at=datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
    )


def test_identical_participants_same_minute_is_confident():
    rows = detect_orphans(
        [
            _cand("api", emails=["a@dlthub.com", "b@acme.com"], actor="api-token"),
            _cand("sys", emails=["a@dlthub.com", "b@acme.com"], actor="system"),
        ],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.orphan_meeting_id == "api"
    assert row.canonical_system_meeting_id == "sys"
    assert row.overlap == 1.0
    assert row.confidence == "confident"


def test_partial_overlap_is_review():
    # Share 1 of 2 participants → overlap 0.5 → review band, not confident.
    rows = detect_orphans(
        [
            _cand("api", emails=["a@dlthub.com", "x@other.com"], actor="api-token"),
            _cand("sys", emails=["a@dlthub.com", "b@acme.com"], actor="system"),
        ],
    )
    assert len(rows) == 1
    assert rows[0].overlap == 0.5
    assert rows[0].confidence == "review"


def test_no_system_in_bucket_yields_no_orphan():
    rows = detect_orphans(
        [_cand("api", emails=["a@dlthub.com", "b@acme.com"], actor="api-token")],
    )
    assert rows == []


def test_system_only_bucket_yields_no_orphan():
    rows = detect_orphans(
        [
            _cand("s1", emails=["a@dlthub.com"], actor="system"),
            _cand("s2", emails=["a@dlthub.com"], actor="system"),
        ],
    )
    assert rows == []


def test_different_minute_not_grouped():
    # api-token and system 2 minutes apart are NOT the same slot — no pairing.
    rows = detect_orphans(
        [
            _cand("api", minute_offset=2, emails=["a@x.com"], actor="api-token"),
            _cand("sys", minute_offset=0, emails=["a@x.com"], actor="system"),
        ],
    )
    assert rows == []


def test_best_system_chosen_as_canonical():
    # api-token overlaps two system meetings (1.0 and 0.5) → canonical is the 1.0.
    rows = detect_orphans(
        [
            _cand(
                "api",
                emails=["a@dlthub.com", "b@acme.com"],
                actor="api-token",
            ),
            _cand(
                "sys-full",
                emails=["a@dlthub.com", "b@acme.com"],
                actor="system",
            ),
            _cand(
                "sys-partial",
                emails=["a@dlthub.com", "z@other.com"],
                actor="system",
            ),
        ],
    )
    assert len(rows) == 1
    assert rows[0].canonical_system_meeting_id == "sys-full"
    assert rows[0].overlap == 1.0


def test_same_minute_subset_is_review_not_confident():
    # orphan attendees are a strict SUBSET of the system meeting's — overlap is
    # 1.0 but the sets are not equal, so it could be a different meeting. Must be
    # review, never auto-deletable.
    rows = detect_orphans(
        [
            _cand("api", emails=["a@dlthub.com"], actor="api-token"),
            _cand(
                "sys",
                emails=["a@dlthub.com", "b@acme.com"],
                actor="system",
            ),
        ],
    )
    assert len(rows) == 1
    assert rows[0].overlap == 1.0
    assert rows[0].confidence == "review"


def test_same_minute_superset_is_review_not_confident():
    # orphan attendees are a strict SUPERSET — overlap 1.0, sets unequal → review.
    rows = detect_orphans(
        [
            _cand(
                "api",
                emails=["a@dlthub.com", "b@acme.com", "c@acme.com"],
                actor="api-token",
            ),
            _cand("sys", emails=["a@dlthub.com", "b@acme.com"], actor="system"),
        ],
    )
    assert len(rows) == 1
    assert rows[0].overlap == 1.0
    assert rows[0].confidence == "review"


def test_exact_match_preferred_as_canonical_over_subset():
    # Two same-slot system meetings both score overlap 1.0: one is an exact set
    # match, the other a subset. The exact one wins as canonical AND makes the
    # row confident.
    rows = detect_orphans(
        [
            _cand("api", emails=["a@x.com", "b@x.com"], actor="api-token"),
            _cand("sys-subset", emails=["a@x.com"], actor="system"),
            _cand("sys-exact", emails=["a@x.com", "b@x.com"], actor="system"),
        ],
    )
    assert len(rows) == 1
    assert rows[0].canonical_system_meeting_id == "sys-exact"
    assert rows[0].confidence == "confident"


def test_no_participant_overlap_below_floor_is_dropped():
    rows = detect_orphans(
        [
            _cand("api", emails=["x@other.com", "y@other.com"], actor="api-token"),
            _cand("sys", emails=["a@dlthub.com", "b@acme.com"], actor="system"),
        ],
    )
    assert rows == []


def test_missing_created_by_type_raises():
    # created_by_type is the only reliable actor discriminator; a candidate
    # without it would be silently mis-bucketed, so detection fails fast instead.
    untyped = MeetingCandidate(
        meeting_id="m1",
        title="m1",
        start=START,
        participant_emails=["a@x.com"],
        # created_by_type left None
    )
    with pytest.raises(ValueError, match="created_by_type=None"):
        detect_orphans([untyped])


def test_non_utc_start_buckets_by_same_instant():
    # Same instant expressed in a non-UTC tz must bucket into the same minute as
    # its UTC counterpart, or the orphan/system pair is missed.
    plus_two = timezone(timedelta(hours=2))
    api = MeetingCandidate(
        meeting_id="api",
        title="api",
        start=START.astimezone(plus_two),  # 17:00+02:00 == 15:00Z
        participant_emails=["a@x.com", "b@x.com"],
        created_by_type="api-token",
    )
    system = MeetingCandidate(
        meeting_id="sys",
        title="sys",
        start=START,  # 15:00Z
        participant_emails=["a@x.com", "b@x.com"],
        created_by_type="system",
    )
    rows = detect_orphans([api, system])
    assert len(rows) == 1
    assert rows[0].confidence == "confident"


def test_classify_split_counts():
    rows = detect_orphans(
        [
            # confident slot
            _cand("api1", emails=["a@x.com", "b@x.com"], actor="api-token"),
            _cand("sys1", emails=["a@x.com", "b@x.com"], actor="system"),
            # review slot (2 min later, 0.5 overlap)
            _cand(
                "api2",
                minute_offset=2,
                emails=["c@x.com", "p@other.com"],
                actor="api-token",
            ),
            _cand(
                "sys2",
                minute_offset=2,
                emails=["c@x.com", "d@x.com"],
                actor="system",
            ),
        ],
    )
    confident, review = classify(rows)
    assert len(confident) == 1
    assert len(review) == 1
    assert confident[0].orphan_meeting_id == "api1"
    assert review[0].orphan_meeting_id == "api2"


def test_attio_url_format():
    rows = detect_orphans(
        [
            _cand("api", emails=["a@x.com"], actor="api-token"),
            _cand("sys", emails=["a@x.com"], actor="system"),
        ],
    )
    assert rows[0].attio_url == "https://app.attio.com/dlthub/meetings/record/api"


def test_csv_columns_and_files(tmp_path):
    rows = detect_orphans(
        [
            _cand("api", emails=["a@x.com", "b@x.com"], actor="api-token"),
            _cand("sys", emails=["a@x.com", "b@x.com"], actor="system"),
        ],
    )
    paths = write_orphan_csvs(rows, tmp_path)
    assert paths["confident"].exists()
    assert paths["review"].exists()
    assert paths["all"].exists()

    with paths["all"].open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == CSV_FIELDNAMES
        data = list(reader)
    assert len(data) == 1
    assert data[0]["orphan_meeting_id"] == "api"
    assert data[0]["canonical_system_meeting_id"] == "sys"
    assert data[0]["confidence"] == "confident"
    assert data[0]["overlap"] == "1.00"
    assert data[0]["attio_url"].endswith("/meetings/record/api")
