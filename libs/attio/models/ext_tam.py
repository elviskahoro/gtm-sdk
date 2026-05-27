from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class ExtTamInput(BaseModel):
    """Input payload for the ``ext_tam`` custom object.

    ``connection_created_date`` is a custom ``ext_tam`` attribute (type=date)
    holding the source-of-truth start date for this AE×account
    relationship. NOT the same as Attio's built-in record ``created_at``,
    which is set by the server at write time and cannot be overridden via
    the public API (empirically verified 2026-05-26).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    person_self_id: str
    employer_id: str
    account_ids: list[str] = Field(min_length=1)
    customer_region: str | None = None
    customer_district: str | None = None
    coverage_type: str | None = None
    last_connection_date: date | None = None
    connection_created_date: date | None = None
    partner_score: float | None = None
    internal_score: float | None = None
    source: str
    source_snapshot_date: date
