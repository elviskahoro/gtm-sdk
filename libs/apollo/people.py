from __future__ import annotations

from typing import Any

from libs.apollo.client import get_client
from libs.apollo.models import PersonEnrichInput, PersonSearchInput


def enrich_person(input: PersonEnrichInput) -> dict[str, Any]:
    client = get_client()
    kwargs: dict[str, Any] = {}
    if input.email:
        kwargs["email"] = input.email
    if input.first_name:
        kwargs["first_name"] = input.first_name
    if input.last_name:
        kwargs["last_name"] = input.last_name
    if input.name:
        kwargs["name"] = input.name
    if input.domain:
        kwargs["domain"] = input.domain
    if input.linkedin_url:
        kwargs["linkedin_url"] = input.linkedin_url
    if input.organization_name:
        kwargs["organization_name"] = input.organization_name
    response = client.people.enrichment(**kwargs)
    return response.model_dump()


def search_people(input: PersonSearchInput) -> dict[str, Any]:
    client = get_client()
    kwargs: dict[str, Any] = {
        "page": input.page,
        "per_page": input.per_page,
    }
    if input.q_keywords:
        kwargs["q_keywords"] = input.q_keywords
    if input.person_titles:
        kwargs["person_titles"] = input.person_titles
    if input.person_seniorities:
        kwargs["person_seniorities"] = input.person_seniorities
    if input.person_locations:
        kwargs["person_locations"] = input.person_locations
    if input.q_organization_domains_list:
        kwargs["q_organization_domains_list"] = input.q_organization_domains_list
    if input.organization_locations:
        kwargs["organization_locations"] = input.organization_locations
    if input.organization_num_employees_ranges:
        kwargs["organization_num_employees_ranges"] = (
            input.organization_num_employees_ranges
        )
    response = client.people.search(**kwargs)
    return response.model_dump()
