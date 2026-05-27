"""Tests for Exa model validation."""

import pytest
from pydantic import ValidationError

from libs.exa.models import SearchInput


def test_num_results_bounds():
    """Test num_results validation (1-100 bounds)."""
    # Valid: 1
    SearchInput(query="test", num_results=1)
    # Valid: 100
    SearchInput(query="test", num_results=100)
    # Valid: 50
    SearchInput(query="test", num_results=50)

    # Invalid: 0
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(query="test", num_results=0)
    assert "num_results must be between 1 and 100" in str(exc_info.value)

    # Invalid: 101
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(query="test", num_results=101)
    assert "num_results must be between 1 and 100" in str(exc_info.value)


def test_category_restriction_start_published_date():
    """Test that start_published_date is rejected with category=company."""
    # Valid without category restriction
    SearchInput(query="test", start_published_date="2025-01-01")

    # Invalid with company category
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(
            query="test",
            category="company",
            start_published_date="2025-01-01",
        )
    assert "start_published_date" in str(exc_info.value)

    # Invalid with people category
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(
            query="test",
            category="people",
            start_published_date="2025-01-01",
        )
    assert "start_published_date" in str(exc_info.value)


def test_category_restriction_end_published_date():
    """Test that end_published_date is rejected with category=company/people."""
    # Valid without category restriction
    SearchInput(query="test", end_published_date="2025-12-31")

    # Invalid with company category
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(
            query="test",
            category="company",
            end_published_date="2025-12-31",
        )
    assert "end_published_date" in str(exc_info.value)


def test_category_restriction_exclude_domains():
    """Test that exclude_domains is rejected with category=company/people."""
    # Valid without category restriction
    SearchInput(query="test", exclude_domains=["spam.com"])

    # Invalid with company category
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(
            query="test",
            category="company",
            exclude_domains=["spam.com"],
        )
    assert "exclude_domains" in str(exc_info.value)


def test_extra_forbid_rejects_unknown_fields():
    """Test that extra="forbid" rejects unknown or deprecated fields."""
    # Valid
    SearchInput(query="test", type="auto", num_results=10)

    # Deprecated field: use_autoprompt (camelCase variant)
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(query="test", use_autoprompt=True)  # type: ignore
    assert "use_autoprompt" in str(exc_info.value) or "extra_forbidden" in str(
        exc_info.value,
    )

    # Deprecated field: num_sentences
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(query="test", num_sentences=5)  # type: ignore
    assert "num_sentences" in str(exc_info.value) or "extra_forbidden" in str(
        exc_info.value,
    )

    # Deprecated field: highlights_per_url
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(query="test", highlights_per_url=3)  # type: ignore
    assert "highlights_per_url" in str(exc_info.value) or "extra_forbidden" in str(
        exc_info.value,
    )

    # Deprecated field: tokens_num
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(query="test", tokens_num=100)  # type: ignore
    assert "tokens_num" in str(exc_info.value) or "extra_forbidden" in str(
        exc_info.value,
    )

    # Deprecated field: livecrawl
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(query="test", livecrawl=True)  # type: ignore
    assert "livecrawl" in str(exc_info.value) or "extra_forbidden" in str(
        exc_info.value,
    )

    # Unknown field: typo
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(query="test", queryz="typo")  # type: ignore
    assert "queryz" in str(exc_info.value) or "extra_forbidden" in str(
        exc_info.value,
    )


def test_include_domains_strips_whitespace():
    """Regression (roborev): ``include_domains`` entries are normalized in the
    model so all entry points (CLI flag, ``--json``, direct construction) get
    the same cleanup, not just the CLI flag path."""
    si = SearchInput(query="x", include_domains=["  a.com ", "b.com"])
    assert si.include_domains == ["a.com", "b.com"]


def test_include_domains_rejects_blank_entries():
    with pytest.raises(ValidationError, match="non-empty string"):
        SearchInput(query="x", include_domains=["a.com", "   "])


def test_exclude_domains_strips_and_rejects_blank():
    si = SearchInput(query="x", exclude_domains=[" c.com "])
    assert si.exclude_domains == ["c.com"]
    with pytest.raises(ValidationError, match="non-empty string"):
        SearchInput(query="x", exclude_domains=[""])


def test_include_domains_rejects_empty_list():
    """Regression (roborev): an explicitly empty domain list is a caller
    bug, not "no filter". Send None / omit the field instead."""
    with pytest.raises(ValidationError, match="non-empty when set"):
        SearchInput(query="x", include_domains=[])
    with pytest.raises(ValidationError, match="non-empty when set"):
        SearchInput(query="x", exclude_domains=[])


def test_multiple_restrictions_together():
    """Test multiple category restrictions together."""
    # All three restrictions should fail together
    with pytest.raises(ValidationError) as exc_info:
        SearchInput(
            query="test",
            category="company",
            start_published_date="2025-01-01",
            end_published_date="2025-12-31",
            exclude_domains=["spam.com"],
        )
    error_str = str(exc_info.value)
    assert "start_published_date" in error_str or "end_published_date" in error_str
