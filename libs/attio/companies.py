from typing import Any

from libs.attio.client import get_client
from libs.attio.errors import (
    AttioConflictError,
    AttioNotFoundError,
    AttioValidationError,
)
from libs.attio.models import CompanyInput, CompanyResult, CompanySearchResult
from libs.attio.sdk_boundary import (
    build_patch_record_request,
    build_post_record_request,
    extract_existing_record_id,
    is_uniqueness_conflict,
    model_dump_or_empty,
)
from libs.attio.values import (
    format_company_description,
    format_company_domains,
    format_company_name,
)


def _build_values(input: CompanyInput, partial: bool = False) -> dict[str, Any]:
    values: dict[str, Any] = {}

    if input.name:
        values["name"] = format_company_name(input.name)

    domains = format_company_domains(input.domain)
    if domains:
        values["domains"] = domains

    description = format_company_description(input.description)
    if description:
        values["description"] = description

    return values


def _extract_result(data: Any, created: bool) -> CompanyResult:
    raw: dict[str, Any] = model_dump_or_empty(data)
    record_id: str = data.id.record_id

    name: str | None = None
    domains: list[str] = []

    name_values = data.values.get("name", [])
    for nv in name_values:
        if hasattr(nv, "value"):
            name = nv.value
            break

    domain_values = data.values.get("domains", [])
    for dv in domain_values:
        if hasattr(dv, "domain"):
            domains.append(dv.domain)

    return CompanyResult(
        record_id=record_id,
        name=name,
        domains=domains,
        created=created,
        raw=raw,
    )


def _extract_search_result(data: Any) -> CompanySearchResult:
    record_id: str = data.id.record_id

    name: str | None = None
    for nv in data.values.get("name", []):
        if hasattr(nv, "value"):
            name = nv.value
            break

    domains: list[str] = []
    for dv in data.values.get("domains", []):
        if hasattr(dv, "domain"):
            domains.append(dv.domain)

    description: str | None = None
    for desc in data.values.get("description", []):
        if hasattr(desc, "value"):
            description = desc.value
            break

    return CompanySearchResult(
        record_id=record_id,
        name=name,
        domains=domains,
        description=description,
    )


def search_companies(
    name: str | None = None,
    domain: str | None = None,
    limit: int = 25,
) -> list[CompanySearchResult]:
    conditions: list[dict[str, Any]] = []
    if name:
        conditions.append({"name": {"$contains": name}})
    if domain:
        conditions.append({"domains": domain})

    if not conditions:
        raise AttioValidationError("Provide at least one search criterion.")

    if len(conditions) == 1:
        filter_: dict[str, Any] = conditions[0]
    else:
        filter_ = {"$and": conditions}

    with get_client() as client:
        response = client.records.post_v2_objects_object_records_query(
            object="companies",
            filter_=filter_,
            limit=limit,
        )
        return [_extract_search_result(record) for record in response.data]


def add_company(input: CompanyInput) -> CompanyResult:
    values = _build_values(input)
    with get_client() as client:
        try:
            response = client.records.post_v2_objects_object_records(
                object="companies",
                data=build_post_record_request(values),
            )

        except Exception as e:
            if is_uniqueness_conflict(e):
                existing_id = extract_existing_record_id(e)
                raise AttioConflictError(
                    "Company already exists. Use 'update' instead."
                    + (f" Existing record ID: {existing_id}" if existing_id else ""),
                    existing_record_id=existing_id,
                ) from e
            raise

        return _extract_result(response.data, created=True)


def update_company(
    record_id: str | None,
    domain: str | None,
    input: CompanyInput,
) -> CompanyResult:
    with get_client() as client:
        if not record_id:
            if not domain:
                raise AttioNotFoundError(
                    "Provide --id or --domain to identify the company.",
                )
            query_response = client.records.post_v2_objects_object_records_query(
                object="companies",
                filter_={"domains": domain},
                limit=1,
            )
            if not query_response.data:
                raise AttioNotFoundError(
                    f"No company found with domain: {domain}",
                )
            record_id = query_response.data[0].id.record_id

        values = _build_values(input, partial=True)
        response = client.records.patch_v2_objects_object_records_record_id_(
            object="companies",
            record_id=record_id,
            data=build_patch_record_request(values),
        )

        return _extract_result(response.data, created=False)
