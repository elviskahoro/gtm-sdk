"""Fireflies recording → Attio operation vocabulary.

This is the Fireflies twin of ``src/fathom/webhook/call.py::attio_get_operations``:
it emits the same source-agnostic ``UpsertMeeting`` (+ optional summary
``UpsertNote``) ops. Like Fathom, Fireflies carries no real calendar iCalUID, so
the meeting sets ``match_existing_by_participants=True``: the dispatcher first
resolves an existing Attio meeting by participants + start window (catching the
~17k calendar-synced rows and any Fathom/Cal.com record for the same meeting)
before falling back to a find-or-create on the synthetic ``canonical_meeting_uid``
(host+start-minute). Without this, every Fireflies recording also on a synced
calendar would mint a duplicate ``dlt-mtg-`` meeting — the exact bug ai-4bz (#205)
fixed for the live Fathom/Cal.com webhooks. Running it twice is a no-op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from libs.meetings import canonical_meeting_uid
from libs.parsers.constants import EMAIL_DOMAINS_TO_KEEP
from src.attio.ops import (
    CompanyRef,
    MeetingExternalRef,
    MeetingParticipant,
    MeetingRef,
    PersonRef,
    UpsertMeeting,
    UpsertNote,
)
from src.fireflies.utils import (
    build_meeting_description,
    fireflies_summary_markdown,
    select_note_parent_email,
)

if TYPE_CHECKING:
    from libs.fireflies import FirefliesRecording
    from src.attio.ops import AttioOp

# Internal hosts whose domains are *not* treated as external CRM companies to
# link a meeting to. Mirrors ``src/octolens/backfill._DLTHUB_HOSTS``; kept local
# to avoid coupling two ``src`` modules.
DEFAULT_ORG_DOMAINS: frozenset[str] = frozenset({"dlthub.com", "dlt.run"})

# Consumer/free mailbox domains. A meeting attendee on one of these is a person,
# not a company account, so we must not mint a CompanyRef for the domain — that
# would emit unresolved-link noise and could attach the meeting to a junk
# "gmail.com" company if one exists. Reuses the parser's curated list.
_PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset(EMAIL_DOMAINS_TO_KEEP)

SUMMARY_NOTE_TITLE = "Fireflies summary"


def to_attio_operations(
    rec: FirefliesRecording,
    *,
    include_notes: bool = True,
    org_domains: frozenset[str] = DEFAULT_ORG_DOMAINS,
) -> list[AttioOp]:
    """Build the Attio ops for one Fireflies recording."""
    summary_markdown = fireflies_summary_markdown(rec)

    # Participants: the union of attendees and the host. The host is always
    # included (not just as an empty-attendees fallback) so the meeting always
    # has an organizer and the note-parent host fallback is always available,
    # even when a transcript's meeting_attendees omits the host. Sorted for
    # deterministic note-parent selection across reruns. Fireflies carries no
    # RSVP signal, so MeetingParticipant.status keeps its "accepted" default —
    # not trustworthy, same caveat as Fathom.
    participant_emails = sorted({*rec.attendee_emails, rec.host_email})
    participants = [
        MeetingParticipant(
            email_address=email,
            is_organizer=(email == rec.host_email),
        )
        for email in participant_emails
    ]

    ical_uid = canonical_meeting_uid(host_email=rec.host_email, start=rec.start)

    # Link the meeting to existing Attio records so it surfaces on the related
    # people/company timelines. Refs are resolved by the dispatcher at write time
    # and silently dropped if absent (link-only — the /v2/meetings POST itself
    # auto-creates participant Persons). Company links cover external attendee
    # domains only; our own org domains are not CRM companies.
    person_links: list[PersonRef] = [
        PersonRef(attribute="email", value=email) for email in participant_emails
    ]
    excluded_domains = org_domains | _PERSONAL_EMAIL_DOMAINS
    company_domains = sorted(
        {
            domain
            for email in participant_emails
            if (domain := email.rsplit("@", 1)[-1]) not in excluded_domains
        },
    )
    company_links: list[CompanyRef] = [
        CompanyRef(domain=domain) for domain in company_domains
    ]

    ops: list[AttioOp] = [
        UpsertMeeting(
            external_ref=MeetingExternalRef(
                ical_uid=ical_uid,
                provider="google",
                is_recurring=False,
            ),
            title=rec.title,
            description=build_meeting_description(
                rec,
                summary_markdown=summary_markdown,
            ),
            start=rec.start,
            end=rec.end,
            is_all_day=False,
            participants=participants,
            linked_records=[*person_links, *company_links],
            # Fireflies has no calendar iCalUID, so dedupe against the existing
            # calendar-synced / Fathom / Cal.com meeting by participants + start
            # window before falling back to the synthetic ical_uid. Mirrors the
            # Fathom path (src/fathom/webhook/call.py); see ai-4bz / #205.
            match_existing_by_participants=True,
        ),
    ]

    if include_notes and summary_markdown.strip():
        note_parent = PersonRef(
            attribute="email",
            value=select_note_parent_email(
                participant_emails=participant_emails,
                host_email=rec.host_email,
                org_domains=org_domains,
            ),
        )
        ops.append(
            UpsertNote(
                parent=note_parent,
                meeting=MeetingRef(ical_uid=ical_uid),
                title=SUMMARY_NOTE_TITLE,
                content=summary_markdown,
            ),
        )

    return ops
