import sys
from typing import Any

from libs.attio.client import get_client
from libs.attio.errors import AttioNotFoundError, AttioValidationError
from libs.attio.models import NoteInput, NoteResult
from libs.attio.sdk_boundary import build_post_note_request, model_dump_or_empty


def _resolve_parent_record_id(
    client: Any,
    parent_object: str,
    parent_record_id: str | None,
    parent_email: str | None,
    parent_domain: str | None,
) -> str:
    if parent_record_id:
        return parent_record_id

    if parent_object == "people":
        if not parent_email:
            raise AttioValidationError(
                "Provide --record-id or --email to identify the parent person.",
            )
        response = client.records.post_v2_objects_object_records_query(
            object="people",
            filter_={"email_addresses": parent_email},
            limit=1,
        )
        if not response.data:
            raise AttioNotFoundError(
                f"No person found with email: {parent_email}",
            )
        return response.data[0].id.record_id

    if parent_object == "companies":
        if not parent_domain:
            raise AttioValidationError(
                "Provide --record-id or --domain to identify the parent company.",
            )
        response = client.records.post_v2_objects_object_records_query(
            object="companies",
            filter_={"domains": parent_domain},
            limit=1,
        )
        if not response.data:
            raise AttioNotFoundError(
                f"No company found with domain: {parent_domain}",
            )
        return response.data[0].id.record_id

    raise AttioValidationError(
        f"Invalid parent object: {parent_object}. Must be 'people' or 'companies'.",
    )


def _extract_result(note: Any) -> NoteResult:
    raw: dict[str, Any] = model_dump_or_empty(note)
    return NoteResult(
        note_id=note.id.note_id,
        title=note.title,
        parent_object=note.parent_object,
        parent_record_id=note.parent_record_id,
        content_plaintext=note.content_plaintext,
        created_at=note.created_at,
        raw=raw,
    )


def add_note(input: NoteInput) -> NoteResult:
    with get_client() as client:
        record_id = _resolve_parent_record_id(
            client,
            input.parent_object,
            input.parent_record_id,
            input.parent_email,
            input.parent_domain,
        )

        response = client.notes.post_v2_notes(
            data=build_post_note_request(
                parent_object=input.parent_object,
                parent_record_id=record_id,
                title=input.title,
                format_=input.format,
                content=input.content,
            ),
        )

        return _extract_result(response.data)


def update_note(note_id: str, input: NoteInput) -> NoteResult:
    with get_client() as client:
        existing = client.notes.get_v2_notes_note_id_(note_id=note_id)
        note = existing.data

        title = input.title if input.title else note.title
        content = input.content if input.content else note.content_plaintext
        format_ = input.format

        client.notes.delete_v2_notes_note_id_(note_id=note_id)

        response = client.notes.post_v2_notes(
            data=build_post_note_request(
                parent_object=note.parent_object,
                parent_record_id=note.parent_record_id,
                title=title,
                format_=format_,
                content=content,
            ),
        )

        new_note_id = response.data.id.note_id
        print(
            f"Warning: note_id changed from {note_id} to {new_note_id} "
            f"(Attio does not support note updates in place).",
            file=sys.stderr,
        )

        return _extract_result(response.data)
