"""Fathom webhook payload model (2026 schema).

Validated against 19 real samples. Fathom sends one webhook per recording
with all four sections (transcript, action_items, default_summary,
crm_matches) populated together — sections may be empty but are always present.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from webhooks.fathom.action_item import ActionItem
from webhooks.fathom.calendar_invitee import CalendarInvitee
from webhooks.fathom.crm_match import CrmMatches
from webhooks.fathom.recorded_by import RecordedBy
from webhooks.fathom.summary import DefaultSummary
from webhooks.fathom.transcript import TranscriptMessage


class Webhook(BaseModel):
    recording_id: int
    url: str
    share_url: str
    title: str
    meeting_title: str
    transcript_language: str
    created_at: datetime
    scheduled_start_time: datetime
    scheduled_end_time: datetime
    recording_start_time: datetime
    recording_end_time: datetime
    calendar_invitees_domains_type: str

    recorded_by: RecordedBy
    default_summary: DefaultSummary
    crm_matches: CrmMatches

    calendar_invitees: list[CalendarInvitee]
    action_items: list[ActionItem]
    transcript: list[TranscriptMessage]
