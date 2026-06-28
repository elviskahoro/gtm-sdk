"""Detect pre-fix synthetic duplicate Attio meetings for manual cleanup (ai-4bz.9).

Before the dedup fix landed (ai-4bz.8), the live cal.com webhook and a partial
backfill minted ``api-token`` Meeting records that shadow the real
calendar-synced ``system`` meetings at the same slot. They cannot be removed via
the API — Attio's ``meetings`` object is a beta, GET-only surface (no DELETE), and
``meetings`` is not a standard object so ``DELETE /v2/objects/meetings/...`` 404s.
Deletion is therefore manual in the Attio UI. This module produces the list the
operator works from.

An **orphan** is an ``api-token`` meeting that shares the same start-MINUTE and a
participant-set overlap with a ``system`` meeting at that slot. Results split by
confidence so a bulk delete never touches an ambiguous row:

- ``confident`` — EXACT participant-set equality at the same minute: a true
  shadow of the system meeting. SAFE to delete.
- ``review``    — overlap 0.50–1.0 without exact equality (subset/superset
  attendee sets, or two *different* meetings sharing a slot and one attendee —
  both possible false positives). DO NOT bulk-delete; judge by hand.

Pure and side-effect-free (no Attio/network calls) so it unit-tests offline — the
caller (``scripts/attio-find-orphan-meetings.py``) supplies the listed meetings.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from libs.attio.models import MeetingCandidate
from src.attio.meeting_match import overlap_coefficient

# Minimum participant overlap for an api-token meeting to count as a duplicate of
# a same-slot system meeting. Matches the live matcher's ``_MIN_OVERLAP`` so both
# use one definition of "structurally the same meeting".
REVIEW_MIN = 0.50
# A row is ``confident`` (safe to bulk-delete) ONLY on EXACT participant-set
# equality at the same minute. The overlap coefficient is 1.0 for any
# subset/superset too (Attio may capture a different attendee count than the
# calendar), and a same-minute meeting with merely a subset of the attendees can
# be a DIFFERENT meeting — so subset/superset matches stay in ``review``.

_ATTIO_RECORD_URL = "https://app.attio.com/dlthub/meetings/record/{meeting_id}"

CSV_FIELDNAMES = [
    "orphan_meeting_id",
    "canonical_system_meeting_id",
    "participants",
    "created_at",
    "title",
    "overlap",
    "confidence",
    "attio_url",
]


@dataclass(frozen=True)
class OrphanRow:
    """One api-token duplicate paired with the system meeting it shadows."""

    orphan_meeting_id: str
    canonical_system_meeting_id: str
    participants: str  # ";"-joined sorted lowercased emails of the orphan
    created_at: str  # orphan ``created_at`` ISO, or "" if unknown
    title: str
    overlap: float
    confidence: str  # "confident" | "review"

    @property
    def attio_url(self) -> str:
        return _ATTIO_RECORD_URL.format(meeting_id=self.orphan_meeting_id)

    def as_csv_dict(self) -> dict[str, str]:
        return {
            "orphan_meeting_id": self.orphan_meeting_id,
            "canonical_system_meeting_id": self.canonical_system_meeting_id,
            "participants": self.participants,
            "created_at": self.created_at,
            "title": self.title,
            "overlap": f"{self.overlap:.2f}",
            "confidence": self.confidence,
            "attio_url": self.attio_url,
        }


def _start_minute_key(dt: datetime) -> tuple[int, int, int, int, int]:
    """Truncate a start to the UTC minute — the slot two meetings must share.

    Normalizes to UTC first so candidates carrying a non-UTC tz (or none) bucket
    by the same wall-clock instant; a naive datetime is assumed already-UTC.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute)


def detect_orphans(meetings: Iterable[MeetingCandidate]) -> list[OrphanRow]:
    """Pair each api-token meeting with the system meeting it duplicates.

    Groups by start-minute, then within each slot scores every api-token meeting
    against every system meeting by participant overlap. An api-token meeting is
    an orphan iff its best system match clears ``REVIEW_MIN``; the highest-overlap
    system meeting (ties broken by smallest meeting_id, for determinism) is its
    canonical. Meetings with no same-slot system match are dropped — not orphans.

    INPUT CONTRACT: every candidate MUST have ``created_by_type`` populated — it
    is the only field that distinguishes the three relevant actor classes
    (``system`` canonical rows, ``api-token`` deletion candidates, and other
    types we ignore). ``created_by_system`` alone cannot, so a candidate with
    ``created_by_type=None`` would be silently mis-bucketed; we raise instead.
    Build candidates via ``libs.attio.iter_meetings_in_range``, which populates
    it. Raises :class:`ValueError` on the first untyped candidate.
    """
    by_slot: dict[
        tuple[int, int, int, int, int],
        tuple[list[MeetingCandidate], list[MeetingCandidate]],
    ] = defaultdict(lambda: ([], []))
    for m in meetings:
        if m.created_by_type is None:
            msg = (
                f"MeetingCandidate {m.meeting_id!r} has created_by_type=None; "
                "orphan detection cannot classify it as a system row or an "
                "api-token duplicate. Build candidates via "
                "libs.attio.iter_meetings_in_range, which populates the field."
            )
            raise ValueError(msg)
        api_token, system = by_slot[_start_minute_key(m.start)]
        if m.created_by_type == "system":
            system.append(m)
        elif m.created_by_type == "api-token":
            api_token.append(m)
        # Other actor types (workspace-member, app) are neither candidate
        # orphans nor canonical system rows — ignore.

    rows: list[OrphanRow] = []
    for api_token, system in by_slot.values():
        if not api_token or not system:
            continue
        for orphan in api_token:
            orphan_emails = set(orphan.participant_emails)
            # Every same-slot system meeting clearing the overlap floor, tagged
            # with whether its participant set is EXACTLY the orphan's (the
            # confident signal — overlap 1.0 alone also covers subset/superset).
            qualifying = [
                (
                    overlap,
                    orphan_emails == sys_emails,
                    sys_meeting.meeting_id,
                )
                for sys_meeting in system
                for sys_emails in [set(sys_meeting.participant_emails)]
                for overlap in [overlap_coefficient(orphan_emails, sys_emails)]
                if overlap >= REVIEW_MIN
            ]
            if not qualifying:
                continue
            # Canonical = the strongest claim: prefer an exact-set match, then
            # higher overlap, then smallest meeting_id (deterministic re-runs).
            overlap, exact, canonical_id = min(
                qualifying,
                key=lambda q: (not q[1], -q[0], q[2]),
            )
            rows.append(
                OrphanRow(
                    orphan_meeting_id=orphan.meeting_id,
                    canonical_system_meeting_id=canonical_id,
                    participants=";".join(orphan.participant_emails),
                    created_at=(
                        orphan.created_at.isoformat() if orphan.created_at else ""
                    ),
                    title=orphan.title,
                    overlap=overlap,
                    confidence="confident" if exact else "review",
                ),
            )
    return rows


def classify(rows: list[OrphanRow]) -> tuple[list[OrphanRow], list[OrphanRow]]:
    """Split rows into ``(confident, review)``."""
    confident = [r for r in rows if r.confidence == "confident"]
    review = [r for r in rows if r.confidence == "review"]
    return confident, review


def _write_csv(path: Path, rows: list[OrphanRow]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_csv_dict())


def write_orphan_csvs(rows: list[OrphanRow], out_dir: Path) -> dict[str, Path]:
    """Write ``orphans_confident.csv`` / ``orphans_review.csv`` / ``orphans.csv``.

    The combined ``orphans.csv`` keeps the ``confidence`` column so it is
    self-describing. Returns the written paths keyed by ``confident``/``review``/
    ``all``. Files are overwritten on each run (the scan is read-only/idempotent).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    confident, review = classify(rows)
    paths = {
        "confident": out_dir / "orphans_confident.csv",
        "review": out_dir / "orphans_review.csv",
        "all": out_dir / "orphans.csv",
    }
    _write_csv(paths["confident"], confident)
    _write_csv(paths["review"], review)
    _write_csv(paths["all"], rows)
    return paths
