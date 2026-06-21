"""Cross-provider meeting identity (ai-4bz model).

Previously Cal.com and Fathom both wrote the synthetic
``canonical_meeting_uid(host, start)`` so a single POST collapsed them onto one
Attio meeting. That minted a DUPLICATE of the calendar-synced meeting (Attio's
Google/Outlook integration owns the real meeting under the real iCalUID).

New model:
  - **Cal.com** carries the real calendar iCalUID (``icsUid``) as the meeting
    ``external_ref.ical_uid`` (kept for api-token replay idempotency + the
    create-fallback uid), but the calendar-synced ``system`` meeting Attio owns
    exposes no matchable ``ical_uid`` — so ``icsUid`` alone mints a duplicate.
    Cal.com therefore ALSO sets ``match_existing_by_participants`` (ai-4bz.8) so
    the dispatcher first resolves the existing calendar meeting by participants +
    start window, mirroring the Fathom/Fireflies paths.
  - **Fathom** has no calendar uid, so it keeps the canonical hash (as the
    in-plan LookupTable key + create fallback) and sets
    ``match_existing_by_participants`` so the dispatcher resolves the existing
    meeting by participants + start window (``src.attio.meeting_match``).

Convergence onto one record is therefore a DISPATCH-time behavior, covered by
``tests/src/attio/test_meeting_match.py``. These tests pin the per-provider op
shape that makes it possible.
"""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest

from libs.meetings import canonical_meeting_uid
from src.attio.ops import UpsertMeeting
from src.caldotcom.webhook.booking import Webhook as CalcomBookingWebhook
from src.fathom.webhook.call import Webhook as FathomCallWebhook

pytestmark = pytest.mark.integration

CALCOM_FIXTURE = Path("api/samples/caldotcom.booking.created.redacted.json")
FATHOM_FIXTURE = Path("api/samples/fathom.recording.redacted.json")

HOST_EMAIL = "host@dlthub.com"
SHARED_START = "2026-06-01T15:00:00.000Z"
SHARED_END = "2026-06-01T15:30:00.000Z"
# The real calendar uid carried by the cal.com fixture (api/samples).
CALCOM_ICS_UID = "ical-evt-abc123@cal.com"


def _calcom_webhook() -> CalcomBookingWebhook:
    payload = orjson.loads(CALCOM_FIXTURE.read_bytes())
    payload["payload"]["start"] = SHARED_START
    payload["payload"]["end"] = SHARED_END
    payload["payload"]["hosts"] = [
        {
            "id": 1,
            "name": "Host",
            "email": HOST_EMAIL,
            "displayEmail": HOST_EMAIL,
            "username": "host",
            "timeZone": "UTC",
        },
    ]
    return CalcomBookingWebhook.model_validate(payload)


def _fathom_webhook() -> FathomCallWebhook:
    payload = orjson.loads(FATHOM_FIXTURE.read_bytes())
    payload["scheduled_start_time"] = SHARED_START
    payload["scheduled_end_time"] = SHARED_END
    payload["recorded_by"]["email"] = HOST_EMAIL
    return FathomCallWebhook.model_validate(payload)


def _meeting_op(webhook: CalcomBookingWebhook | FathomCallWebhook) -> UpsertMeeting:
    return next(
        op for op in webhook.attio_get_operations() if isinstance(op, UpsertMeeting)
    )


def test_calcom_meeting_keys_on_real_ical_uid() -> None:
    """Cal.com retains the real calendar uid AND uses the participant matcher.

    ``icsUid`` is kept for api-token replay idempotency + the create-fallback uid,
    but the calendar-synced ``system`` row exposes no matchable ``ical_uid``, so
    ``match_existing_by_participants`` must be set to collapse onto it (ai-4bz.8).
    """
    op = _meeting_op(_calcom_webhook())
    assert op.external_ref.ical_uid == CALCOM_ICS_UID
    assert op.match_existing_by_participants is True


def test_fathom_meeting_uses_canonical_uid_and_participant_match() -> None:
    """Fathom keeps the canonical hash but resolves the real meeting by participants."""
    op = _meeting_op(_fathom_webhook())
    assert op.external_ref.ical_uid == canonical_meeting_uid(
        host_email=HOST_EMAIL,
        start=_fathom_webhook().scheduled_start_time,
    )
    assert op.match_existing_by_participants is True


def test_fathom_canonical_uid_is_host_case_insensitive() -> None:
    """Fathom's create-fallback uid normalizes host-email case (canonical hash)."""
    fathom = _fathom_webhook()
    fathom.recorded_by.email = HOST_EMAIL.upper()
    assert _meeting_op(fathom).external_ref.ical_uid == canonical_meeting_uid(
        host_email=HOST_EMAIL,
        start=fathom.scheduled_start_time,
    )
