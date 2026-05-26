"""Partner-agnostic CRUD for the Attio ``ext_tam`` custom object.

No Snowflake-specific or dltHub-specific names should appear here. Snowflake
CSV → ExtTamInput mapping lives in ``projects/snowflake_tam_loader/``.

The Attio public REST API does NOT honor ``created_at`` on record POST or
PUT (verified empirically 2026-05-26 via raw HTTP probe; the SDK's
OpenAPI-generated request model exposes only ``values``). The CSV's
"connection created date" is preserved as the custom attribute
``connection_created_date`` (type=date), NOT as the record's built-in
``created_at`` metadata.
"""

from __future__ import annotations

from typing import Any

from libs.attio.client import get_client
from libs.attio.contracts import ReliabilityEnvelope
from libs.attio.models import ExtTamInput
from libs.attio.sdk_boundary import (
    build_patch_record_request,
    build_post_record_request,
)

EXT_TAM_OBJECT = "ext_tam"


def _record_ref(target_object: str, record_id: str) -> dict[str, str]:
    return {"target_object": target_object, "target_record_id": record_id}


def _build_ext_tam_values(input: ExtTamInput) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": [{"value": input.name}],
        "person_self": [_record_ref("people", input.person_self_id)],
        "employer": [_record_ref("companies", input.employer_id)],
        "accounts": [_record_ref("companies", aid) for aid in input.account_ids],
        "source": [input.source],
        "source_snapshot_date": [{"value": input.source_snapshot_date.isoformat()}],
    }
    if input.customer_region:
        values["customer_region"] = [input.customer_region]
    if input.customer_district:
        values["customer_district"] = [input.customer_district]
    if input.coverage_type:
        values["coverage_type"] = [input.coverage_type]
    if input.last_connection_date is not None:
        values["last_connection_date"] = [
            {"value": input.last_connection_date.isoformat()},
        ]
    if input.connection_created_date is not None:
        values["connection_created_date"] = [
            {"value": input.connection_created_date.isoformat()},
        ]
    if input.partner_score is not None:
        values["partner_score"] = [{"value": input.partner_score}]
    if input.internal_score is not None:
        values["internal_score"] = [{"value": input.internal_score}]
    return values


def find_by_person_and_account(
    *,
    person_id: str,
    account_id: str,
) -> str | None:
    """Return the ``ext_tam.record_id`` matching this AE + covered-account pair, or None.

    The natural key for ``ext_tam`` is (person_self, accounts[0]). Attio does
    not expose composite unique attributes; we filter both sides and pick the
    first match. Two rows with the same pair indicate prior data drift —
    callers can pick the lexicographically smallest record_id and warn, but
    for the v1 loader the first match wins.
    """
    filter_: dict[str, Any] = {
        "$and": [
            {"person_self": {"target_record_id": person_id}},
            {"accounts": {"target_record_id": account_id}},
        ],
    }
    with get_client() as client:
        response = client.records.post_v2_objects_object_records_query(
            object=EXT_TAM_OBJECT,
            filter_=filter_,
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


def upsert_ext_tam(
    *,
    input: ExtTamInput,
    apply: bool,
) -> ReliabilityEnvelope:
    """Upsert an ``ext_tam`` record keyed on (person_self, accounts[0]).

    Preview mode (``apply=False``) does no IO and returns ``noop``.

    On ``apply=True``: POST when no existing record matches; PATCH when one
    matches. The CSV's ``connection_created_date`` is stored in the
    ``ext_tam.connection_created_date`` custom attribute (NOT the built-in
    record ``created_at``, which Attio does not allow overriding).
    """
    if not apply:
        return _envelope(action="noop", record_id=None, meta={"preview": True})

    primary_account = input.account_ids[0]
    existing = find_by_person_and_account(
        person_id=input.person_self_id,
        account_id=primary_account,
    )

    values = _build_ext_tam_values(input)

    with get_client() as client:
        if existing is None:
            response = client.records.post_v2_objects_object_records(
                object=EXT_TAM_OBJECT,
                data=build_post_record_request(values),
            )
            return _envelope(action="created", record_id=response.data.id.record_id)

        response = client.records.patch_v2_objects_object_records_record_id_(
            object=EXT_TAM_OBJECT,
            record_id=existing,
            data=build_patch_record_request(values),
        )
        return _envelope(action="updated", record_id=response.data.id.record_id)
