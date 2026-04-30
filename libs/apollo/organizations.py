from __future__ import annotations

from typing import Any

from libs.apollo.client import get_client
from libs.apollo.models import OrgEnrichInput, OrgSearchInput


def enrich_organization(input: OrgEnrichInput) -> dict[str, Any]:
    client = get_client()
    response = client.organizations.enrich(domain=input.domain)
    return response.model_dump()


def search_organizations(input: OrgSearchInput) -> dict[str, Any]:
    client = get_client()
    kwargs: dict[str, Any] = {
        "page": input.page,
        "per_page": input.per_page,
    }
    if input.q_keywords:
        kwargs["q_organization_name"] = input.q_keywords
    if input.organization_locations:
        kwargs["organization_locations"] = input.organization_locations
    if input.organization_num_employees_ranges:
        kwargs["organization_num_employees_ranges"] = (
            input.organization_num_employees_ranges
        )
    response = client.organizations.search(**kwargs)
    return response.model_dump()
