from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AttributeCreateResult(BaseModel):
    mode: Literal["preview", "apply"]
    attribute_title: str
    attribute_slug: str
    attribute_type: str
    attribute_exists: bool
    attribute_created: bool
