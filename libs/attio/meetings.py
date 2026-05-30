from __future__ import annotations

import logging
from typing import Any

from attio.errors.sdkerror import SDKError

from libs.attio.client import get_client
from libs.attio.contracts import ErrorEntry, ReliabilityEnvelope
from libs.attio.errors import AttioNotFoundError, classify_error
from libs.attio.models import MeetingInput, MeetingResult
from libs.attio.sdk_boundary import build_post_meeting_request
from libs.attio.values import build_meeting_payload

logger = logging.getLogger(__name__)


def _extract_result(data: Any) -> MeetingResult:
    ident = data.id
    ical_uid: str | None = None
    ref = getattr(data, "external_ref", None)
    if ref is not None:
        ical_uid = getattr(ref, "ical_uid", None)
    return MeetingResult(
        meeting_id=ident.meeting_id,
        workspace_id=ident.workspace_id,
        title=getattr(data, "title", ""),
        external_ref_ical_uid=ical_uid,
        created=True,
    )


def _post_meeting(input: MeetingInput) -> MeetingResult:
    payload = build_meeting_payload(input)["data"]
    with get_client() as client:
        try:
            response = client.meetings.post_v2_meetings(
                data=build_post_meeting_request(
                    external_ref=payload["external_ref"],
                    title=payload["title"],
                    description=payload["description"],
                    start=payload["start"],
                    end=payload["end"],
                    is_all_day=payload["is_all_day"],
                    participants=payload["participants"],
                    linked_records=payload["linked_records"],
                ),
            )
        except SDKError as exc:
            # Translate a bare 404 into a typed, self-explanatory error rather
            # than letting classify_error fall through to `unknown_error` (which
            # reads like a code bug). Attio's meetings feature is ALPHA and is
            # NOT provisioned in the dev workspace: GET /v2/meetings returns 200
            # (empty) but POST 404s. Meeting creation is only verifiable against
            # the prod workspace. Same SDK-404 idiom as libs/attio/attributes.py.
            # See ai-h5y.
            status = getattr(getattr(exc, "raw_response", None), "status_code", None)
            if status == 404:
                raise AttioNotFoundError(
                    "Attio POST /v2/meetings returned 404. The meetings feature "
                    "(ALPHA) is not provisioned in this Attio workspace — known "
                    "for the dev workspace, where GET /v2/meetings returns 200 "
                    "(empty) but POST 404s. Meeting creation is only verifiable "
                    "against the prod Attio workspace. See ai-h5y.",
                ) from exc
            raise
        return _extract_result(response.data)


def find_or_create_meeting(input: MeetingInput) -> ReliabilityEnvelope:
    """POST /v2/meetings.

    Attio's endpoint is 'Find or create a meeting' — repeat POSTs that share
    the same `external_ref.ical_uid` return the existing record.

    Workspace caveat (ai-h5y): the meetings feature is ALPHA and is provisioned
    only in the prod Attio workspace. Against the dev workspace this POST 404s
    (even though GET /v2/meetings returns 200 empty and the key has
    meeting:read-write scope), surfacing here as a `not_found` failure envelope.
    Meeting creation is therefore only verifiable against prod — not a code bug.
    """
    try:
        result = _post_meeting(input)
    except Exception as exc:  # noqa: BLE001 — boundary
        classified = classify_error(exc)
        logger.warning("attio_meeting_failed", extra={"code": classified.code})
        return ReliabilityEnvelope(
            success=False,
            partial_success=False,
            action="failed",
            record_id=None,
            errors=[
                ErrorEntry(
                    code=classified.code,
                    message=classified.message,
                    error_type=classified.error_type,
                    fatal=classified.fatal,
                    field=classified.field,
                ),
            ],
            warnings=[],
            skipped_fields=[],
            meta={"output_schema_version": "v1"},
        )

    return ReliabilityEnvelope(
        success=True,
        partial_success=False,
        action="created",
        record_id=result.meeting_id,
        errors=[],
        warnings=[],
        skipped_fields=[],
        meta={
            "output_schema_version": "v1",
            "external_ref_ical_uid": result.external_ref_ical_uid,
        },
    )
