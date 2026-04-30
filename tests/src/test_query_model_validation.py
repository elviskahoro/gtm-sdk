from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.attio.people import PersonAddQuery, PersonSearchQuery


def test_person_add_query_rejects_extra_fields():
    with pytest.raises(ValidationError) as exc_info:
        PersonAddQuery(email="a@b.com", company_name="Acme")  # type: ignore[call-arg]
    errors = exc_info.value.errors()
    assert any(e["type"] == "extra_forbidden" for e in errors)


def test_person_add_query_has_no_api_key_field():
    q = PersonAddQuery(email="a@b.com")
    assert not hasattr(q, "attio_api_key")


def test_person_search_query_accepts_valid_fields():
    q = PersonSearchQuery(name="Ada")
    assert q.name == "Ada"
    assert q.limit == 25
