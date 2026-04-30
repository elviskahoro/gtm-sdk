from __future__ import annotations

from libs.attio.client import get_client
from libs.attio.models import AttributeCreateResult


def create_companies_attribute(
    *,
    title: str,
    api_slug: str,
    attribute_type: str = "select",
    description: str | None = None,
    is_multiselect: bool = True,
    is_required: bool = False,
    is_unique: bool = False,
    apply: bool,
) -> AttributeCreateResult:
    with get_client() as client:
        attributes_response = client.attributes.get_v2_target_identifier_attributes(
            target="objects",
            identifier="companies",
        )

        existing_attribute_slugs = {
            getattr(attr, "api_slug", "") for attr in attributes_response.data
        }
        attribute_exists = api_slug in existing_attribute_slugs
        attribute_created = False

        if not attribute_exists and apply:
            payload = {
                "title": title,
                "description": description or "",
                "api_slug": api_slug,
                "type": attribute_type,
                "is_required": is_required,
                "is_unique": is_unique,
                "is_multiselect": is_multiselect,
                "config": {},
            }

            client.attributes.post_v2_target_identifier_attributes(
                target="objects",
                identifier="companies",
                data=payload,
            )
            attribute_created = True

    return AttributeCreateResult(
        mode="apply" if apply else "preview",
        attribute_title=title,
        attribute_slug=api_slug,
        attribute_type=attribute_type,
        attribute_exists=attribute_exists,
        attribute_created=attribute_created,
    )
