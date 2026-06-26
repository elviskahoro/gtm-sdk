from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from attio.errors.sdkerror import SDKError

from libs.attio.client import get_client
from libs.attio.contracts import ReliabilityEnvelope
from libs.attio.errors import AttioNotFoundError, classify_error
from libs.attio.models import MeetingCandidate, MeetingInput, MeetingResult
from libs.attio.sdk_boundary import build_post_meeting_request
from libs.attio.values import build_meeting_payload

logger = logging.getLogger(__name__)


def _meeting_start_dt(meeting: Any) -> datetime | None:
    """Pull a tz-aware UTC start from a listed Meeting (datetime or all-day date)."""
    start = getattr(meeting, "start", None)
    raw = getattr(start, "datetime_", None) or getattr(start, "date_", None)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def list_candidate_meetings(
    *,
    start: datetime,
    window_minutes: int = 10,
    limit: int = 50,
) -> list[MeetingCandidate]:
    """List meetings whose start falls within ``±window_minutes`` of ``start``.

    Used to match a source meeting (e.g. a Fathom recording) to an existing
    Attio meeting when the caller lacks the calendar ``ical_uid``. The list
    endpoint filters server-side by an overlap window (``starts_before`` /
    ``ends_from``); we then keep only candidates whose START is within the
    window (a long meeting can overlap without starting near ``start``).
    Participant scoring is left to the caller (``src.attio.meeting_match``) so
    this stays a pure single-object Attio adapter.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    window = timedelta(minutes=window_minutes)
    lo, hi = start - window, start + window

    def iso(d: datetime) -> str:
        return d.isoformat().replace("+00:00", "Z")

    candidates: list[MeetingCandidate] = []
    cursor: str | None = None
    with get_client() as client:
        while True:
            resp = client.meetings.get_v2_meetings(
                limit=limit,
                cursor=cursor,
                starts_before=iso(hi),
                ends_from=iso(lo),
                sort="start_asc",
            )
            for m in resp.data:
                m_start = _meeting_start_dt(m)
                if m_start is None or not (lo <= m_start <= hi):
                    continue
                emails = sorted(
                    (p.email_address or "").lower()
                    for p in (m.participants or [])
                    if getattr(p, "email_address", None)
                )
                actor = getattr(m, "created_by_actor", None)
                candidates.append(
                    MeetingCandidate(
                        meeting_id=m.id.meeting_id,
                        title=m.title or "",
                        start=m_start,
                        participant_emails=emails,
                        created_by_system=getattr(actor, "type", None) == "system",
                    ),
                )
            cursor = resp.pagination.next_cursor if resp.pagination else None
            if not cursor:
                break
    return candidates


def _extract_result(data: Any) -> MeetingResult:
    ident = data.id
    ical_uid: str | None = None
    ref = getattr(data, "external_ref", None)
    if ref is not None:
        ical_uid = getattr(ref, "ical_uid", None)
    return MeetingResult(
        meeting_id=ident.meeting_id,
        workspace_id=ident.workspace_id,
        title=getattr(data, "title", ""),
        external_ref_ical_uid=ical_uid,
        created=True,
    )


def _post_meeting(input: MeetingInput) -> MeetingResult:
    payload = build_meeting_payload(input)["data"]
    with get_client() as client:
        try:
            response = client.meetings.post_v2_meetings(
                data=build_post_meeting_request(
                    external_ref=payload["external_ref"],
                    title=payload["title"],
                    description=payload["description"],
                    start=payload["start"],
                    end=payload["end"],
                    is_all_day=payload["is_all_day"],
                    participants=payload["participants"],
                    linked_records=payload["linked_records"],
                ),
            )
        except SDKError as exc:
            # Translate a bare 404 into a typed, self-explanatory error rather
            # than letting classify_error fall through to `unknown_error` (which
            # reads like a code bug). Attio's meetings feature is ALPHA and is
            # NOT provisioned in the dev workspace: GET /v2/meetings returns 200
            # (empty) but POST 404s. Meeting creation is only verifiable against
            # the prod workspace. Same SDK-404 idiom as libs/attio/attributes.py.
            # See ai-h5y.
            status = getattr(getattr(exc, "raw_response", None), "status_code", None)
            if status == 404:
                raise AttioNotFoundError(
                    "Attio POST /v2/meetings returned 404. The meetings feature "
                    "(ALPHA) is not provisioned in this Attio workspace — known "
                    "for the dev workspace, where GET /v2/meetings returns 200 "
                    "(empty) but POST 404s. Meeting creation is only verifiable "
                    "against the prod Attio workspace. See ai-h5y.",
                ) from exc
            raise
        return _extract_result(response.data)


def find_or_create_meeting(input: MeetingInput) -> ReliabilityEnvelope:
    """POST /v2/meetings.

    Attio's endpoint is 'Find or create a meeting' — repeat POSTs that share
    the same `external_ref.ical_uid` return the existing record.

    Workspace caveat (ai-h5y): the meetings feature is ALPHA and is provisioned
    only in the prod Attio workspace. Against the dev workspace this POST 404s
    (even though GET /v2/meetings returns 200 empty and the key has
    meeting:read-write scope), surfacing here as a `not_found` failure envelope.
    Meeting creation is therefore only verifiable against prod — not a code bug.
    """
    try:
        result = _post_meeting(input)
    except Exception as exc:  # noqa: BLE001 — boundary
        classified = classify_error(exc)
        logger.warning("attio_meeting_failed", extra={"code": classified.code})
        return ReliabilityEnvelope(
            success=False,
            partial_success=False,
            action="failed",
            record_id=None,
            errors=[classified.to_error_entry()],
            warnings=[],
            skipped_fields=[],
            meta={"output_schema_version": "v1"},
        )

    return ReliabilityEnvelope(
        success=True,
        partial_success=False,
        action="created",
        record_id=result.meeting_id,
        errors=[],
        warnings=[],
        skipped_fields=[],
        meta={
            "output_schema_version": "v1",
            "external_ref_ical_uid": result.external_ref_ical_uid,
        },
    )
