from __future__ import annotations

from pydantic import BaseModel


class Assignee(BaseModel):
    email: str | None
    name: str
    team: str | None


class ActionItem(BaseModel):
    assignee: Assignee
    completed: bool
    description: str
    recording_playback_url: str
    recording_timestamp: str
    user_generated: bool
