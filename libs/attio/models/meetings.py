from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MeetingExternalRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ical_uid: str
    provider: Literal["google", "outlook"] = "google"
    is_recurring: bool = False
    original_start_time: str | None = None


class MeetingParticipantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_address: str
    is_organizer: bool
    status: Literal["accepted", "tentative", "declined", "pending"] = "accepted"


class MeetingLinkedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object: str
    record_id: str


class MeetingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_ref: MeetingExternalRef
    title: str
    description: str
    start: datetime
    end: datetime
    is_all_day: bool = False
    participants: list[MeetingParticipantInput]
    linked_records: list[MeetingLinkedRecord] = Field(default_factory=list)


class MeetingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meeting_id: str
    workspace_id: str
    title: str
    external_ref_ical_uid: str | None = None
    created: bool = True


class MeetingCandidate(BaseModel):
    """A possible match for a source meeting, returned by the list query.

    Attio's ``GET /v2/meetings`` does NOT expose ``external_ref.ical_uid`` (only
    create/find responses echo it), so a producer that lacks the calendar uid
    (e.g. Fathom) cannot match by uid. It matches structurally instead — start
    time + participant emails — against these candidates. See
    ``src.attio.meeting_match``.
    """

    model_config = ConfigDict(extra="forbid")

    meeting_id: str
    title: str
    start: datetime
    participant_emails: list[str] = Field(default_factory=list)
    # True when Attio's calendar integration created this row (``created_by_actor.
    # type == "system"``). The matcher prefers these — they are the canonical
    # calendar-synced meetings we want to attach to, over any api-token duplicates
    # a prior run may have minted (ai-4bz).
    created_by_system: bool = False
