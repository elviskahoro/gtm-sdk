from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class CompanyInput(BaseModel):
    name: str
    domain: str | None = None
    description: str | None = None
    industry: str | None = None
    employee_count: str | None = None
    estimate_revenue: str | None = None
    linkedin_url: str | None = None


class CompanyResult(BaseModel):
    record_id: str
    name: str | None = None
    domains: list[str] = []
    created: bool = False
    raw: dict[str, Any] = {}


class CompanySearchResult(BaseModel):
    record_id: str
    name: str | None = None
    domains: list[str] = []
    description: str | None = None
