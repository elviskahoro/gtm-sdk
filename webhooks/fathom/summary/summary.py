from __future__ import annotations

from pydantic import BaseModel


class DefaultSummary(BaseModel):
    markdown_formatted: str
    template_name: str
