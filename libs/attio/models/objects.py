from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ObjectCreateResult(BaseModel):
    mode: Literal["preview", "apply"]
    api_slug: str
    object_exists: bool
    object_created: bool
