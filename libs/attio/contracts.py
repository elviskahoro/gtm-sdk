from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class WarningEntry(BaseModel):
    code: str
    message: str
    field: str | None = None
    retryable: bool = False


class SkippedField(BaseModel):
    field: str
    reason: str


class ErrorEntry(BaseModel):
    code: str
    message: str
    error_type: str
    fatal: bool
    field: str | None = None
    details: dict[str, Any] = {}


class ReliabilityEnvelope(BaseModel):
    success: bool
    partial_success: bool
    action: Literal["searched", "created", "updated", "noop", "failed"]
    record_id: str | None
    warnings: list[WarningEntry] = []
    skipped_fields: list[SkippedField] = []
    errors: list[ErrorEntry] = []
    meta: dict[str, Any] = {}
