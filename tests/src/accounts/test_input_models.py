from __future__ import annotations

from src.accounts.models import (
    BatchAddCompaniesInput,
    BatchAddPeopleInput,
    EnrichInput,
    FindPeopleInput,
    MapAccountHierarchyInput,
    ResearchInput,
)


def test_research_input():
    inp = ResearchInput(objective="find acme")
    assert inp.objective == "find acme"


def test_enrich_input():
    inp = EnrichInput(url="https://acme.com", objective="funding")
    assert inp.url == "https://acme.com"


def test_find_people_input():
    inp = FindPeopleInput(query="vp sales")
    assert inp.query == "vp sales"


def test_map_account_hierarchy_input():
    inp = MapAccountHierarchyInput(account="acme.com")
    assert inp.account == "acme.com"


def test_batch_add_people_input_defaults():
    inp = BatchAddPeopleInput(records=[{"email": "a@b.com"}])
    assert inp.apply is False


def test_batch_add_companies_input_defaults():
    inp = BatchAddCompaniesInput(records=[{"name": "Acme", "domain": "acme.com"}])
    assert inp.apply is False
