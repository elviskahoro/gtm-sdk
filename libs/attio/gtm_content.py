"""Source-agnostic CRUD for the Attio ``gtm_content`` custom object.

No CMS-specific names should appear here. Blog-folder → GtmContentInput
mapping lives in ``projects/crm-uploader/src/gtm_content/``.

Upserts are keyed on the ``slug`` attribute (the CMS's stable id). Attio
does not enforce uniqueness on ``slug`` in the current schema, so the
find-then-write pattern mirrors ``ext_tam``: filter, first match wins,
POST when absent / PATCH when present.
"""

from __future__ import annotations

from typing import Any

from libs.attio.client import get_client
from libs.attio.contracts import ReliabilityEnvelope
from libs.attio.models import GtmContentInput
from libs.attio.sdk_boundary import (
    build_patch_record_request,
    build_post_record_request,
)

GTM_CONTENT_OBJECT = "gtm_content"


def _record_ref(target_object: str, record_id: str) -> dict[str, str]:
    return {"target_object": target_object, "target_record_id": record_id}


def _build_gtm_content_values(input: GtmContentInput) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": [{"value": input.name}],
        "slug": [{"value": input.slug}],
        "content_type": [input.content_type],
    }
    if input.url:
        values["url"] = [{"value": input.url}]
    if input.published_date is not None:
        values["published_date"] = [{"value": input.published_date.isoformat()}]
    if input.status:
        values["status"] = [input.status]
    if input.description:
        values["description"] = [{"value": input.description}]
    if input.topics:
        values["topics"] = list(input.topics)
    if input.author_ids:
        values["authors"] = [_record_ref("people", pid) for pid in input.author_ids]
    if input.company_ids:
        values["companies_featured"] = [
            _record_ref("companies", cid) for cid in input.company_ids
        ]
    return values


def find_by_slug(slug: str) -> str | None:
    """Return the ``gtm_content.record_id`` whose ``slug`` matches, or None."""
    with get_client() as client:
        response = client.records.post_v2_objects_object_records_query(
            object=GTM_CONTENT_OBJECT,
            filter_={"slug": slug},
            limit=2,
        )
        if not response.data:
            return None
        return response.data[0].id.record_id


def _envelope(
    *,
    action: str,
    record_id: str | None,
    meta: dict[str, Any] | None = None,
) -> ReliabilityEnvelope:
    return ReliabilityEnvelope(
        success=True,
        partial_success=False,
        action=action,  # type: ignore[arg-type]
        record_id=record_id,
        warnings=[],
        skipped_fields=[],
        errors=[],
        meta={"output_schema_version": "v1", **(meta or {})},
    )


def upsert_gtm_content(
    *,
    input: GtmContentInput,
    apply: bool,
) -> ReliabilityEnvelope:
    """Upsert a ``gtm_content`` record keyed on ``slug``.

    Preview mode (``apply=False``) does no IO and returns ``noop``.
    On ``apply=True``: POST when no record has this slug; PATCH otherwise.
    Select values (``content_type``, ``status``, ``topics``) must already be
    seeded options — callers run ``ensure_select_options`` first or every
    write 422s with ``value_not_found`` (the ai-3gx failure mode).
    """
    if not apply:
        return _envelope(action="noop", record_id=None, meta={"preview": True})

    existing = find_by_slug(input.slug)
    values = _build_gtm_content_values(input)

    with get_client() as client:
        if existing is None:
            response = client.records.post_v2_objects_object_records(
                object=GTM_CONTENT_OBJECT,
                data=build_post_record_request(values),
            )
            return _envelope(action="created", record_id=response.data.id.record_id)

        response = client.records.patch_v2_objects_object_records_record_id_(
            object=GTM_CONTENT_OBJECT,
            record_id=existing,
            data=build_patch_record_request(values),
        )
        return _envelope(action="updated", record_id=response.data.id.record_id)
