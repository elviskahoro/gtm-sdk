from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MentionInput(BaseModel):
    """Input payload for the ``social_mention`` custom object.

    Unlike CompanyInput / PersonInput / MeetingInput which target Attio's
    built-in standard objects, this model targets a **custom** object that
    must be bootstrapped into the workspace via
    ``scripts/attio-social_mentions-bootstrap.py`` before any upsert succeeds.

    Fields here mirror the webhook-writable attributes only. The CRM-owned
    fields (triage_status, related_person, related_company) are intentionally
    absent so the webhook path cannot overwrite them. However, related_person_record_id
    is passed by the dispatcher when a linked Person record exists (e.g., from
    LinkedIn sources) and is used only to build the mention values.
    """

    mention_url: str
    last_action: Literal["mention_created", "mention_updated"]
    source_platform: str
    source_id: str
    mention_title: str | None = None
    mention_body: str
    mention_timestamp: datetime
    author_handle: str
    author_profile_url: str | None = None
    author_avatar_url: str | None = None
    relevance_score: str | None = None
    relevance_comment: str | None = None
    primary_keyword: str
    keywords: list[str] = Field(default_factory=list)
    octolens_tags: list[str] = Field(default_factory=list)
    sentiment: Literal["Positive", "Neutral", "Negative"] | None = None
    language: str | None = None
    subreddit: str | None = None
    view_id: int | None = None
    view_name: str | None = None
    bookmarked: bool = False
    image_url: str | None = None
    related_person_record_id: str | None = None
