from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


class GtmContentInput(BaseModel):
    """Input payload for the ``gtm_content`` custom object.

    ``slug`` is the natural key for upserts: it is the CMS's stable
    identifier and survives URL changes (the canonical ``url`` can move
    between domains/paths while the slug stays fixed).

    ``content_type`` and ``status`` must already exist as select options on
    the workspace (seeded by the bootstrap's closed vocabularies) or the
    write 422s with ``value_not_found``. ``topics`` is an open vocabulary —
    callers seed new options via ``ensure_select_options`` before writing.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    slug: str
    content_type: str
    url: str | None = None
    published_date: date | None = None
    status: str | None = None
    description: str | None = None
    topics: list[str] = []
    author_ids: list[str] = []
    company_ids: list[str] = []
