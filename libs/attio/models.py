from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CompanyInput(BaseModel):
    name: str
    domain: str | None = None
    description: str | None = None


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


class NoteInput(BaseModel):
    title: str
    content: str
    parent_object: str
    parent_record_id: str | None = None
    parent_email: str | None = None
    parent_domain: str | None = None
    format: str = "plaintext"


class NoteResult(BaseModel):
    note_id: str
    title: str
    parent_object: str
    parent_record_id: str
    content_plaintext: str
    created_at: str
    raw: dict[str, Any] = {}


class PersonInput(BaseModel):
    email: str
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    location: str | None = None
    company_domain: str | None = None
    notes: str | None = None
    strict: bool = False
    location_mode: Literal["raw", "city"] = "city"
    additional_emails: list[str] = Field(default_factory=list)
    replace_emails: bool = False


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


class AttributeCreateResult(BaseModel):
    mode: Literal["preview", "apply"]
    attribute_title: str
    attribute_slug: str
    attribute_type: str
    attribute_exists: bool
    attribute_created: bool
