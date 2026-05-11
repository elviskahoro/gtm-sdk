from __future__ import annotations

from pydantic import BaseModel


class TranscriptSpeaker(BaseModel):
    display_name: str
    matched_calendar_invitee_email: str | None


class TranscriptMessage(BaseModel):
    speaker: TranscriptSpeaker
    text: str
    timestamp: str
