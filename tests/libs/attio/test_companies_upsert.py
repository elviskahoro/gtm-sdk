"""Tests for libs.attio.companies.upsert_company."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from libs.attio.companies import (
    _build_values,  # pyright: ignore[reportPrivateUsage]
    upsert_company,
)
from libs.attio.models import (
    CompanyInput,
    CompanyResult,
    CompanySearchResult,
)


def _search_match(record_id: str, domain: str = "example.com") -> CompanySearchResult:
    return CompanySearchResult(
        record_id=record_id,
        name="Example",
        domains=[domain],
        description=None,
    )


def _company_result(record_id: str) -> CompanyResult:
    return CompanyResult(
        record_id=record_id,
        name="Example",
        domains=["example.com"],
        created=False,
        raw={},
    )


def test_upsert_company_creates_when_no_match() -> None:
    with (
        patch("libs.attio.companies.search_companies", return_value=[]) as search,
        patch(
            "libs.attio.companies.add_company",
            return_value=_company_result("co-1"),
        ) as add,
    ):
        envelope = upsert_company(CompanyInput(name="Example", domain="example.com"))

    search.assert_called_once_with(domain="example.com", limit=50)
    add.assert_called_once()
    assert envelope.success is True
    assert envelope.action == "created"
    assert envelope.record_id == "co-1"


def test_upsert_company_updates_when_single_match() -> None:
    with (
        patch(
            "libs.attio.companies.search_companies",
            return_value=[_search_match("co-7")],
        ),
        patch(
            "libs.attio.companies.update_company",
            return_value=_company_result("co-7"),
        ) as update,
    ):
        envelope = upsert_company(CompanyInput(name="Example", domain="example.com"))

    update.assert_called_once()
    assert update.call_args.kwargs["record_id"] == "co-7"
    assert envelope.success is True
    assert envelope.action == "updated"
    assert envelope.record_id == "co-7"


def test_upsert_company_no_match_falls_back_to_add() -> None:
    add_mock = MagicMock(return_value=_company_result("co-2"))
    with (
        patch("libs.attio.companies.search_companies", return_value=[]) as search,
        patch("libs.attio.companies.add_company", add_mock),
    ):
        envelope = upsert_company(CompanyInput(name="NewCorp", domain=None))

    # No domain → fall back to name search; no match → add.
    search.assert_called_once_with(name="NewCorp", limit=50)
    add_mock.assert_called_once()
    assert envelope.success is True
    assert envelope.action == "created"


def test_upsert_company_multi_match_picks_smallest_record_id() -> None:
    with (
        patch(
            "libs.attio.companies.search_companies",
            return_value=[_search_match("co-z"), _search_match("co-a")],
        ),
        patch(
            "libs.attio.companies.update_company",
            return_value=_company_result("co-a"),
        ) as update,
    ):
        envelope = upsert_company(CompanyInput(name="Example", domain="example.com"))

    assert update.call_args.kwargs["record_id"] == "co-a"
    assert envelope.partial_success is True
    assert any(
        w.code == "upsert_multi_match_selected_record" for w in envelope.warnings
    )


def test_build_values_emits_domains_when_domain_set() -> None:
    values = _build_values(CompanyInput(name="Acme", domain="acme.com"))
    assert values["domains"] == [{"domain": "acme.com"}]


def test_build_values_omits_domains_when_domain_absent() -> None:
    values = _build_values(CompanyInput(name="Acme", domain=None))
    assert "domains" not in values


def test_build_values_emits_linkedin_for_company_url() -> None:
    values = _build_values(
        CompanyInput(
            name="Acme",
            domain="acme.com",
            linkedin_url="https://www.linkedin.com/company/acme/",
        ),
    )
    assert values["linkedin"] == ["https://www.linkedin.com/company/acme"]


def test_build_values_drops_linkedin_when_input_is_profile_url() -> None:
    # Defense in depth: even if a /in/ URL leaks past the rb2b discriminator
    # into CompanyInput.linkedin_url, _build_values must drop it so the
    # Company linkedin slug never gets a profile URL.
    values = _build_values(
        CompanyInput(
            name="Acme",
            domain="acme.com",
            linkedin_url="https://www.linkedin.com/in/bob-jones",
        ),
    )
    assert "linkedin" not in values


def test_build_values_omits_linkedin_when_absent() -> None:
    values = _build_values(CompanyInput(name="Acme", domain="acme.com"))
    assert "linkedin" not in values


def test_upsert_company_prefers_domain_over_name_for_search() -> None:
    # Domain match should bypass name search even when name spelling differs
    # from what's on the existing record. This is the regression ai-21r fixes:
    # before, every visit for the same company under a different name spelling
    # created a duplicate because search was name-only.
    with (
        patch(
            "libs.attio.companies.search_companies",
            return_value=[_search_match("co-canonical", domain="acme.com")],
        ) as search,
        patch(
            "libs.attio.companies.update_company",
            return_value=_company_result("co-canonical"),
        ),
    ):
        envelope = upsert_company(
            CompanyInput(name="Acme, Inc.", domain="acme.com"),
        )

    search.assert_called_once_with(domain="acme.com", limit=50)
    assert envelope.action == "updated"
    assert envelope.record_id == "co-canonical"
