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
    error_type: str  # Python exception class name (e.g. "ResponseValidationError")
    fatal: bool
    field: str | None = None
    # Attio's documented error envelope fields, distinct from error_type above:
    # status_code is the HTTP status (e.g. 400, 429) and type is Attio's error
    # type tag (e.g. "invalid_request_error"). Both are parsed by
    # describe_attio_error() and forwarded via ClassifiedError.to_error_entry();
    # they stay None for non-Attio errors. See ai-fxs.
    status_code: int | None = None
    type: str | None = None
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
