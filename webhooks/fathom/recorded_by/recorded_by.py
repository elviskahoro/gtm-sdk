from __future__ import annotations

from pydantic import BaseModel


class RecordedBy(BaseModel):
    email: str
    email_domain: str
    name: str
    team: str
