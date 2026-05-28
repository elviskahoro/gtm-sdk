from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PersonInput(BaseModel):
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    github_handle: str | None = None
    github_url: str | None = None
    location: str | None = None
    # ISO-3166-1 alpha-2 country code (e.g. "US", "IN") that pairs with
    # ``location``. Required by ``format_location`` to avoid silent
    # misattribution (see ai-sfp). When None, the ``primary_location``
    # write is skipped even if ``location`` is populated.
    country_code: str | None = None
    company_domain: str | None = None
    notes: str | None = None
    strict: bool = False
    location_mode: Literal["raw", "city"] = "city"
    additional_emails: list[str] = Field(default_factory=list)
    replace_emails: bool = False
    title: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None

    @model_validator(mode="after")
    def _require_identity(self) -> PersonInput:
        has_email = self.email and self.email.strip()
        has_linkedin = self.linkedin and self.linkedin.strip()
        has_github = self.github_handle and self.github_handle.strip()
        if not (has_email or has_linkedin or has_github):
            raise ValueError(
                "At least one of 'email', 'linkedin', or 'github_handle' must be set",
            )
        return self


class PersonResult(BaseModel):
    record_id: str
    email_addresses: list[str] = []
    name: str | None = None
    created: bool = False
    raw: dict[str, Any] = {}


class PersonSearchResult(BaseModel):
    record_id: str
    name: str | None = None
    email_addresses: list[str] = []
    phone_numbers: list[str] = []
    linkedin: str | None = None
    location: str | None = None
    company: str | None = None
