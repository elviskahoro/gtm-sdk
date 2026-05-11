from __future__ import annotations

from pydantic import BaseModel


class CrmCompany(BaseModel):
    name: str
    record_url: str


class CrmContact(BaseModel):
    email: str
    name: str
    record_url: str


class CrmMatches(BaseModel):
    companies: list[CrmCompany] = []
    contacts: list[CrmContact] = []
    deals: list[dict] = []  # shape unknown — no populated samples yet
    error: str | None = None
