from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import Any, Protocol, cast


class _ModelDumpLike(Protocol):
    def model_dump(self) -> dict[str, Any]: ...


def _import_attio_sdk_module() -> ModuleType:
    import attio

    return cast(ModuleType, attio)


def _import_attio_models_module() -> object:
    module = _import_attio_sdk_module()
    return getattr(module, "models")


def get_attio_sdk_client_class() -> type[Any]:
    module = _import_attio_sdk_module()
    return cast(type[Any], getattr(module, "SDK"))


def build_post_record_request(values: dict[str, Any]) -> object:
    models = _import_attio_models_module()
    constructor = getattr(models, "PostV2ObjectsObjectRecordsDataRequest")
    return constructor(values=values)


def build_patch_record_request(values: dict[str, Any]) -> object:
    models = _import_attio_models_module()
    constructor = getattr(models, "PatchV2ObjectsObjectRecordsRecordIDDataRequest")
    return constructor(values=values)


def build_assert_record_request(values: dict[str, Any]) -> object:
    """Build the assert (PUT) request body for an idempotent upsert.

    Attio's assert endpoint creates-or-updates a record keyed by a unique
    matching attribute (set via ``matching_attribute`` query parameter on
    the call site, not in the body).
    """
    models = _import_attio_models_module()
    # Verified via `dir(attio.models)` probe: assert/PUT request type is
    # PutV2ObjectsObjectRecordsDataRequest, accepting a `values` kwarg.
    constructor = getattr(models, "PutV2ObjectsObjectRecordsDataRequest")
    return constructor(values=values)


def build_post_note_request(
    *,
    parent_object: str,
    parent_record_id: str,
    title: str,
    format_: str,
    content: str,
    created_at: datetime | None = None,
    meeting_id: str | None = None,
) -> object:
    """Build the Notes POST request body.

    ``created_at`` is optional; when set, it is passed to the SDK model so
    the resulting Note's ``created_at`` reflects the backdated value
    (documented Attio behavior: "if you wish to backdate a note for
    migration or other purposes, you can override with a custom
    ``created_at`` value").

    ``meeting_id`` is optional; when set, the note is associated with an
    existing Attio Meeting (the Notes API's ``meeting_id`` field). This is the
    only supported way to attach a note to a meeting — meetings cannot be a
    note's ``parent_object`` (ai-gez). Omitted when None so the SDK's ``UNSET``
    default applies (no association).
    """
    models = _import_attio_models_module()
    constructor = getattr(models, "PostV2NotesData")
    kwargs: dict[str, Any] = {
        "parent_object": parent_object,
        "parent_record_id": parent_record_id,
        "title": title,
        "format_": format_,
        "content": content,
    }
    if created_at is not None:
        # The SDK accepts either a string or a datetime; pass an ISO string
        # to avoid any timezone normalization surprises.
        kwargs["created_at"] = created_at.isoformat()
    if meeting_id is not None:
        kwargs["meeting_id"] = meeting_id
    return constructor(**kwargs)


def build_post_meeting_request(
    *,
    external_ref: dict[str, Any],
    title: str,
    description: str,
    start: dict[str, Any],
    end: dict[str, Any],
    is_all_day: bool,
    participants: list[dict[str, Any]],
    linked_records: list[dict[str, Any]],
) -> object:
    models = _import_attio_models_module()
    constructor = getattr(models, "PostV2MeetingsData")
    return constructor(
        external_ref=external_ref,
        title=title,
        description=description,
        start=start,
        end=end,
        is_all_day=is_all_day,
        participants=participants,
        linked_records=linked_records,
    )


def extract_exception_body_text(exc: BaseException) -> str:
    body = getattr(exc, "body", None)
    if body is None:
        return str(exc)
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        return body
    return str(body)


def _parse_json_object(text: str) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, dict):
        return cast(Mapping[str, Any], parsed)
    return None


def extract_existing_record_id(exc: BaseException) -> str | None:
    payload = _parse_json_object(extract_exception_body_text(exc))
    if payload is None:
        return None

    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    existing_record = data.get("existing_record")
    if not isinstance(existing_record, Mapping):
        return None
    record_id_obj = existing_record.get("id")
    if not isinstance(record_id_obj, Mapping):
        return None
    record_id = record_id_obj.get("record_id")
    if isinstance(record_id, str) and record_id:
        return record_id
    return None


def is_uniqueness_conflict(exc: BaseException) -> bool:
    body_text = extract_exception_body_text(exc)
    payload = _parse_json_object(body_text)
    if payload is not None:
        # Attio's documented error shape: {"status_code": 400, "type": "invalid_request_error",
        # "code": "uniqueness_conflict", ...}. The SDK's pydantic models don't list
        # "uniqueness_conflict" in the Code Literal, so the SDK raises ResponseValidationError
        # before we ever see the parsed body — we re-parse here to avoid substring false positives.
        return payload.get("code") == "uniqueness_conflict"
    return "uniqueness_conflict" in body_text


def is_unknown_filter_attribute(exc: BaseException) -> bool:
    """True when Attio rejected a query filter referencing an unknown attribute.

    Querying the ``people`` object with a filter slug the workspace schema does
    not define (e.g. ``github_handle`` before it is bootstrapped) makes Attio
    return ``{"status_code": 400, ..., "code": "unknown_filter_attribute_slug"}``.
    The SDK's pydantic Code Literal does not list that value, so it raises
    ``ResponseValidationError`` before we ever see the parsed body — we re-parse
    here, mirroring :func:`is_uniqueness_conflict`. See ai-0ex.

    Matched narrowly on the specific ``unknown_filter_attribute_slug`` marker:
    the broader ``filter_error`` category covers other (recoverable) filter
    validation failures we must NOT degrade into a SchemaMismatchError. The
    substring fallback only applies when the body is not parseable JSON.
    """
    body_text = extract_exception_body_text(exc)
    payload = _parse_json_object(body_text)
    if payload is not None:
        return payload.get("code") == "unknown_filter_attribute_slug"
    return "unknown_filter_attribute_slug" in body_text


@dataclass
class AttioErrorDescription:
    """The real fields from Attio's documented error envelope.

    Attio returns ``{"status_code", "type", "code", "message"}`` on a 4xx, but
    the generated SDK's response models constrain ``code`` to a narrow ``Literal``
    per endpoint (e.g. only ``missing_value``). Any other valid code
    (``value_not_found``, ``rate_limit_exceeded``, ...) fails response
    unmarshalling, so the SDK raises ``ResponseValidationError`` and the real
    status/message is lost behind pydantic's ``"Input should be 'missing_value'"``
    noise. This carries the parsed-back-out truth so callers can log/classify it.
    """

    status_code: int | None
    type: str | None
    code: str | None
    message: str | None


def describe_attio_error(exc: BaseException) -> AttioErrorDescription | None:
    """Re-parse an exception's body into Attio's real error envelope, or ``None``.

    Generalizes the re-parse pattern in :func:`is_uniqueness_conflict` /
    :func:`is_unknown_filter_attribute`: the SDK's ``Code`` Literal omits most of
    Attio's codes, so the body is only legible by reading ``exc.body`` directly
    (set from ``raw_response.text`` on every ``SDKError``). Returns a description
    only when the parsed body carries both a string ``code`` and a string
    ``message`` (Attio's documented shape); returns ``None`` otherwise — e.g. a
    plain pydantic ``ValidationError`` on our own request input has no ``.body``,
    so callers fall back to ``str(exc)`` / the pydantic-unwrap path. See ai-e7s.
    """
    payload = _parse_json_object(extract_exception_body_text(exc))
    if payload is None:
        return None
    code = payload.get("code")
    message = payload.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        return None
    status_code = payload.get("status_code")
    error_type = payload.get("type")
    return AttioErrorDescription(
        status_code=status_code if isinstance(status_code, int) else None,
        type=error_type if isinstance(error_type, str) else None,
        code=code,
        message=message,
    )


def model_dump_or_empty(value: object) -> dict[str, Any]:
    if hasattr(value, "model_dump") and callable(getattr(value, "model_dump")):
        return cast(_ModelDumpLike, value).model_dump()
    return {}
