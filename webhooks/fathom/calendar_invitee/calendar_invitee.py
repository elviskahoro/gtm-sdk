from __future__ import annotations

from pydantic import BaseModel


class CalendarInvitee(BaseModel):
    email: str
    email_domain: str
    is_external: bool
    matched_speaker_display_name: str | None
    name: str
