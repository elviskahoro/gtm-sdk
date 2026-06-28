"""Match a source meeting to an existing Attio meeting without a calendar uid.

Cal.com carries the real calendar iCalUID (``icsUid``) and so dedupes against
calendar-synced meetings directly via ``find_or_create`` (ai-4bz). Fathom does
NOT — its payload has no calendar event id — so it cannot key on the uid. This
module resolves the existing Attio meeting structurally instead: list candidates
in a tight start-time window (``libs.attio.list_candidate_meetings``) and pick
the one whose participants overlap and whose start lines up.

Lives in ``src`` (not ``libs/attio``) because the ambiguity tiebreak may call an
LLM (``libs.openai``), and a ``libs`` adapter must not import another ``libs``.

Safety bias: a WRONG match attaches a recording/notes to the wrong meeting, which
is worse than creating a fresh meeting. So matching is conservative — it requires
a strong participant overlap and returns ``None`` (let the caller create) when no
candidate is clearly the meeting.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from libs.attio.meetings import list_candidate_meetings
from libs.attio.models import MeetingCandidate

logger = logging.getLogger(__name__)

# Overlap coefficient |a∩b| / min(|a|,|b|): 1.0 when one participant set is a
# subset of the other (common — Fathom may capture fewer/more attendees than the
# calendar invite). Require a majority so an unrelated back-to-back meeting in
# the same window is not matched.
_MIN_OVERLAP = 0.5
_DEFAULT_WINDOW_MINUTES = 10


def overlap_coefficient(a: set[str], b: set[str]) -> float:
    """Szymkiewicz–Simpson overlap |a∩b| / min(|a|,|b|); 0.0 if either is empty.

    Shared by the live matcher and the orphan detector (``src.attio.
    orphan_meetings``, ai-4bz.9) so both score participant overlap identically.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# Backwards-compatible internal alias for existing callers in this module.
_overlap_coefficient = overlap_coefficient


def resolve_meeting_id_by_participants(
    *,
    start: datetime,
    participant_emails: list[str],
    title: str = "",
    window_minutes: int = _DEFAULT_WINDOW_MINUTES,
    use_llm_tiebreak: bool = True,
) -> str | None:
    """Return the matching Attio ``meeting_id``, or ``None`` to create a new one.

    Deterministic first: candidates sharing participants are scored by
    (overlap coefficient, start proximity); a clear winner above ``_MIN_OVERLAP``
    wins outright. Only a genuine tie (equal overlap AND equal start proximity —
    two distinct meetings that look identical) falls through to the LLM, which is
    best-effort: if ``OPENAI_API_KEY`` is absent or the call fails, the
    deterministic best candidate is used.
    """
    emails = {e.lower() for e in participant_emails if e}
    if not emails:
        return None

    candidates = list_candidate_meetings(start=start, window_minutes=window_minutes)
    # Score key: (participant overlap, calendar-synced, start proximity). The
    # ``created_by_system`` term steers ties toward the canonical calendar meeting
    # over any api-token duplicate a prior run minted at the same slot (ai-4bz).
    scored: list[tuple[float, bool, float, MeetingCandidate]] = []
    for c in candidates:
        overlap = _overlap_coefficient(emails, set(c.participant_emails))
        if overlap < _MIN_OVERLAP:
            continue
        proximity = -abs((c.start - start).total_seconds())
        scored.append((overlap, c.created_by_system, proximity, c))

    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)

    best_overlap, best_system, best_proximity, best = scored[0]
    tied = [
        c
        for overlap, system, proximity, c in scored
        if (overlap, system, proximity) == (best_overlap, best_system, best_proximity)
    ]
    if len(tied) == 1:
        return best.meeting_id

    logger.info(
        "meeting_match.ambiguous",
        extra={"candidate_count": len(tied), "start": start.isoformat()},
    )
    if use_llm_tiebreak:
        picked = _llm_pick(start=start, title=title, emails=emails, candidates=tied)
        if picked is not None:
            return picked
    # Deterministic fallback: stable, earliest-created-equivalent (smallest id).
    return min(tied, key=lambda c: c.meeting_id).meeting_id


def _llm_pick(
    *,
    start: datetime,
    title: str,
    emails: set[str],
    candidates: list[MeetingCandidate],
) -> str | None:
    """Best-effort LLM disambiguation among equally-scored candidates.

    Returns the chosen ``meeting_id`` or ``None`` (no key / parse failure /
    low confidence) so the caller can fall back deterministically. Never raises.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    valid_ids = {c.meeting_id for c in candidates}
    try:
        import openai

        client = openai.OpenAI(api_key=api_key)
        listing = "\n".join(
            f"- id={c.meeting_id} | title={c.title!r} | start={c.start.isoformat()} "
            f"| participants={c.participant_emails}"
            for c in candidates
        )
        prompt = (
            "Pick the single existing meeting that is the SAME meeting as the "
            "source meeting below. Reply with ONLY the chosen id, or the literal "
            "word NONE if no candidate is clearly the same meeting.\n\n"
            f"Source meeting:\n  title={title!r}\n  start={start.isoformat()}\n"
            f"  participants={sorted(emails)}\n\nCandidates:\n{listing}"
        )
        resp: Any = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        answer = (resp.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001 — best-effort; caller falls back deterministically.
        logger.warning("meeting_match.llm_failed", exc_info=True)
        return None
    return answer if answer in valid_ids else None
