"""BOOKING_CREATED path tests.

The other 6 trigger types are covered in ``test_webhook_fixtures.py``. This
file focuses on the CREATED branch — participant mapping, RSVP status
translation, and the canonical ical_uid derivation that lets Fathom land on the
same Attio Meeting record.

Per the spec at
``design/backlog-202605251625-meeting_state_attrs_on_tracking_events-spec-01.md``,
BOOKING_CREATED now emits FOUR ops: UpsertCompany (host's domain) +
UpsertPerson (host) + UpsertMeeting + EmitMeetingLifecycleEvent. The
host-domain Company gates the host-Person upsert in the dispatcher's
LookupTable. UpsertMeeting is therefore no longer ``ops[0]`` — use
``_find_upsert_meeting`` to extract it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import orjson

from libs.caldotcom.models import BookingCreatedPayload
from libs.meetings import canonical_meeting_uid
from src.attio.ops import (
    AttioOp,
    EmitMeetingLifecycleEvent,
    MeetingExternalRef,
    UpsertMeeting,
)
from src.caldotcom.webhook.booking import Webhook


def _find_upsert_meeting(ops: list[AttioOp]) -> UpsertMeeting:
    matches = [o for o in ops if isinstance(o, UpsertMeeting)]
    assert len(matches) == 1, (
        f"expected exactly 1 UpsertMeeting in plan; got {len(matches)} "
        f"in {[type(o).__name__ for o in ops]}"
    )
    return matches[0]


def _find_lifecycle_event(ops: list[AttioOp]) -> EmitMeetingLifecycleEvent:
    matches = [o for o in ops if isinstance(o, EmitMeetingLifecycleEvent)]
    assert len(matches) == 1, (
        f"expected exactly 1 EmitMeetingLifecycleEvent in plan; got {len(matches)} "
        f"in {[type(o).__name__ for o in ops]}"
    )
    return matches[0]


FIXTURE = Path("api/samples/caldotcom.booking.created.redacted.json")


def _load() -> Webhook:
    payload = orjson.loads(FIXTURE.read_bytes())
    return Webhook.model_validate(payload)


def _mutated_created_webhook(**overrides: object) -> Webhook:
    """Build a CREATED webhook re-validating the payload with field overrides.

    The typed Pydantic payload (post plan-02) is immutable in the sense that
    ``w.payload["x"] = y`` no longer works. Rebuild the envelope dict, mutate
    the payload dict, then re-validate.
    """
    envelope = orjson.loads(FIXTURE.read_bytes())
    payload = envelope["payload"]
    for k, v in overrides.items():
        payload[k] = v
    return Webhook.model_validate(envelope)


def test_required_api_keys_attio_only() -> None:
    """Cal.com webhook declares only ATTIO_API_KEY as required.

    CALCOM_API_KEY is fetched lazily inside ``_calcom_client()`` on the
    BOOKING_NO_SHOW_UPDATED path. Declaring it as required would force
    the other Cal.com event types to fail when CALCOM_API_KEY is missing
    or rotated, even though they never touch Cal.com's API.
    """
    assert Webhook.required_api_keys() == ["ATTIO_API_KEY"]


def test_attio_is_valid_webhook_true_for_normal_payload() -> None:
    assert _load().attio_is_valid_webhook() is True


def test_attio_is_valid_webhook_false_with_no_attendees() -> None:
    w = _mutated_created_webhook(attendees=[])
    assert w.attio_is_valid_webhook() is False
    assert "attendees" in w.attio_get_invalid_webhook_error_msg()


def test_attio_get_operations_returns_single_upsert_meeting() -> None:
    plan = _load().attio_get_operations()

    op = _find_upsert_meeting(plan)
    assert isinstance(op.external_ref, MeetingExternalRef)
    # The Meeting keys on the REAL calendar iCalUID (``icsUid``), NOT the canonical
    # hash. But icsUid can't merge onto the calendar-synced ``system`` row (those
    # expose no matchable external_ref.ical_uid), so dedup happens at dispatch via
    # ``match_existing_by_participants`` (ai-4bz.8 reopen); ``icsUid`` only keys
    # api-token→api-token replay idempotency + the create-fallback uid.
    assert op.external_ref.ical_uid == "ical-evt-abc123@cal.com"
    assert op.external_ref.provider == "google"
    # Resolve the existing calendar meeting by participants before creating one.
    assert op.match_existing_by_participants is True

    # The lifecycle row, however, stays keyed on the canonical hash — it is the
    # PATCH target shared with the cancelled/rescheduled/etc. triggers, which
    # never carry ``icsUid``.
    canonical = canonical_meeting_uid(
        host_email="host@dlthub.com",
        start=datetime.fromisoformat("2026-05-20T15:00:00+00:00"),
    )
    assert _find_lifecycle_event(plan).external_id == canonical

    assert op.title == "Discovery call"
    assert op.description == "Customer would like to discuss pricing."
    assert op.is_all_day is False

    emails = [p.email_address for p in op.participants]
    assert "host@dlthub.com" in emails
    assert "external@example.com" in emails

    organizers = [p for p in op.participants if p.is_organizer]
    assert len(organizers) == 1
    assert organizers[0].email_address == "host@dlthub.com"

    external = next(
        p for p in op.participants if p.email_address == "external@example.com"
    )
    assert external.status == "accepted"


def test_meeting_uses_icsUid_when_present_falls_back_to_canonical() -> None:
    """Meeting external_ref keys on the real calendar iCalUID (``icsUid``).

    Switched from the old "always canonical" behavior (ai-4bz): keying on the
    real ``icsUid`` lets find_or_create collapse onto the meeting Attio's
    calendar sync already created, instead of minting a synthetic duplicate.
    When ``icsUid`` is absent we fall back to the canonical hash so a meeting
    still lands. The lifecycle row's ``external_id`` stays canonical in BOTH
    cases (its key must be stable across triggers that lack ``icsUid``).
    """
    canonical = canonical_meeting_uid(
        host_email="host@dlthub.com",
        start=datetime.fromisoformat("2026-05-20T15:00:00+00:00"),
    )

    with_ics_plan = _load().attio_get_operations()
    with_ics = _find_upsert_meeting(with_ics_plan)
    assert with_ics.external_ref.ical_uid == "ical-evt-abc123@cal.com"
    assert _find_lifecycle_event(with_ics_plan).external_id == canonical

    without_ics_plan = _mutated_created_webhook(icsUid=None).attio_get_operations()
    without_ics = _find_upsert_meeting(without_ics_plan)
    assert without_ics.external_ref.ical_uid == canonical
    assert _find_lifecycle_event(without_ics_plan).external_id == canonical


def test_attio_is_valid_webhook_false_when_host_email_missing() -> None:
    """All host-email fallback sources must be empty for CREATED to fail.

    Regression guard for the broaden-the-gate work (ai-4u6): the previous
    handler had a four-way fallback chain ``hosts → organizer → user →
    userPrimaryEmail`` and real-world Cal.com payloads sometimes omit ``hosts``.
    """
    w = _mutated_created_webhook(
        hosts=[],
        organizer=None,
        user=None,
        userPrimaryEmail=None,
    )
    assert w.attio_is_valid_webhook() is False


def test_attio_creator_email_falls_back_to_organizer() -> None:
    """The creator-email fallback chain drives the canonical lifecycle key.

    With ``hosts=[]`` the host resolves to ``organizer``; that host feeds
    ``canonical_meeting_uid``, which now keys the lifecycle row's ``external_id``
    (the Meeting itself keys on the fixture's real ``icsUid`` — ai-4bz).
    """
    w = _mutated_created_webhook(
        hosts=[],
        organizer={"email": "organizer@example.com"},
    )
    assert w.attio_is_valid_webhook() is True
    plan = w.attio_get_operations()
    assert _find_upsert_meeting(plan).external_ref.ical_uid == "ical-evt-abc123@cal.com"
    assert _find_lifecycle_event(plan).external_id == canonical_meeting_uid(
        host_email="organizer@example.com",
        start=datetime.fromisoformat("2026-05-20T15:00:00+00:00"),
    )


def test_unknown_booking_status_does_not_drop_payload() -> None:
    """Cal.com may add new status strings; permissive ``status: str | None``
    plus ``_caldotcom_status_to_attio`` keeps unknowns mapping to ``accepted``
    instead of failing the discriminator and silently dropping the event."""
    w = _mutated_created_webhook(status="rescheduled_pending_review")
    assert w.attio_is_valid_webhook() is True
    op = _find_upsert_meeting(w.attio_get_operations())
    # Attendee statuses fall back to ``accepted`` for unknown values.
    assert all(p.status == "accepted" for p in op.participants if not p.is_organizer)


def test_hostless_cancelled_organizer_mirrors_booking_status() -> None:
    """Hostless CREATED with status=cancelled should map the organizer
    participant to ``declined`` (matching the prior behavior), not the
    hard-coded ``accepted`` that the first fix shipped with."""
    w = _mutated_created_webhook(
        hosts=[],
        organizer={"email": "organizer@example.com"},
        status="cancelled",
    )
    op = _find_upsert_meeting(w.attio_get_operations())
    organizers = [p for p in op.participants if p.is_organizer]
    assert len(organizers) == 1
    assert organizers[0].status == "declined"


def test_hostless_created_still_adds_organizer_participant() -> None:
    """Host-less BOOKING_CREATED must still emit an organizer participant.

    Previously, when ``hosts[]`` was empty we walked the fallback chain for
    ``ical_uid`` but emitted no organizer participant — so the Attio Meeting
    record was missing the host. Re-add the organizer using the same fallback
    chain ``creator_email`` uses.
    """
    w = _mutated_created_webhook(
        hosts=[],
        organizer={"email": "organizer@example.com"},
    )
    op = _find_upsert_meeting(w.attio_get_operations())
    organizers = [p for p in op.participants if p.is_organizer]
    assert len(organizers) == 1
    assert organizers[0].email_address == "organizer@example.com"


def test_attio_creator_email_falls_back_to_user_then_userPrimaryEmail() -> None:
    """``user.email`` outranks ``userPrimaryEmail``; both outranked by ``organizer``."""
    w_user = _mutated_created_webhook(
        hosts=[],
        organizer=None,
        user={"email": "user@example.com"},
    )
    assert w_user.attio_is_valid_webhook() is True

    w_primary = _mutated_created_webhook(
        hosts=[],
        organizer=None,
        user=None,
        userPrimaryEmail="primary@example.com",
    )
    assert w_primary.attio_is_valid_webhook() is True


def test_attio_is_valid_webhook_false_when_start_missing() -> None:
    """Pydantic-level validation: ``start`` is required on BOOKING_CREATED."""
    envelope = orjson.loads(FIXTURE.read_bytes())
    envelope["payload"].pop("start", None)
    # Discriminator should still find BOOKING_CREATED, but the missing required
    # ``start`` field raises a ValidationError before Webhook is constructed.
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Webhook.model_validate(envelope)


def test_attio_get_operations_marks_absent_attendee_declined() -> None:
    envelope = orjson.loads(FIXTURE.read_bytes())
    envelope["payload"]["attendees"][0]["absent"] = True
    w = Webhook.model_validate(envelope)
    op = _find_upsert_meeting(w.attio_get_operations())
    external = next(
        p for p in op.participants if p.email_address == "external@example.com"
    )
    assert external.status == "declined"


def test_attio_get_operations_maps_cancelled_status_to_declined() -> None:
    """If the BOOKING_CREATED payload arrives with status=cancelled, attendees
    inherit declined. (Edge case — cancellation usually arrives as its own
    BOOKING_CANCELLED webhook, but the field is still on the CREATED model.)
    """
    w = _mutated_created_webhook(status="cancelled")
    op = _find_upsert_meeting(w.attio_get_operations())
    external = next(
        p for p in op.participants if p.email_address == "external@example.com"
    )
    assert external.status == "declined"


def test_lifecycle_event_carries_external_company_domain() -> None:
    """The lifecycle op leads its title with the external attendee's domain.

    Host is ``host@dlthub.com``; the external attendee is
    ``external@example.com`` → ``company_domain == "example.com"``.
    """
    event = _find_lifecycle_event(_load().attio_get_operations())
    assert event.company_domain == "example.com"
    assert event.meeting_title == "Discovery call"
    assert event.event_subtype == "scheduled"


def test_lifecycle_event_domain_none_when_only_host_attends() -> None:
    """No external attendee → company_domain is None (title falls back later)."""
    w = _mutated_created_webhook(
        attendees=[
            {
                "name": "Internal Host",
                "email": "host@dlthub.com",
                "displayEmail": "host@dlthub.com",
                "timeZone": "America/Los_Angeles",
                "absent": False,
            },
        ],
    )
    event = _find_lifecycle_event(w.attio_get_operations())
    assert event.company_domain is None


def test_payload_is_typed_not_dict() -> None:
    """Sanity: post plan-02, ``w.payload`` is a typed BaseModel, not a dict."""
    w = _load()
    assert isinstance(w.payload, BookingCreatedPayload)
