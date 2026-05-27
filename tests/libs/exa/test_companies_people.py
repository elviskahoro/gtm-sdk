"""Tests for Exa convenience wrappers (find_companies, find_people)."""

from unittest.mock import MagicMock, patch

from libs.exa.companies import find_companies
from libs.exa.people import find_people


def test_find_companies_pins_category():
    """Test that find_companies pins category='company'."""
    mock_search = MagicMock()
    mock_search.return_value.cost_dollars = 0.05

    with patch("libs.exa.companies.search", side_effect=mock_search) as mock_fn:
        find_companies("Snowflake")

        # Verify search was called once
        mock_fn.assert_called_once()
        # Get the SearchInput that was passed
        call_args = mock_fn.call_args
        search_input = call_args[0][0]
        assert search_input.category == "company"
        assert search_input.query == "Snowflake"


def test_find_companies_default_num_results():
    """Test that find_companies uses num_results=5 by default."""
    mock_search = MagicMock()
    mock_search.return_value.cost_dollars = 0.05

    with patch("libs.exa.companies.search", side_effect=mock_search) as mock_fn:
        find_companies("test")

        search_input = mock_fn.call_args[0][0]
        assert search_input.num_results == 5


def test_find_companies_custom_num_results():
    """Test that find_companies respects custom num_results."""
    mock_search = MagicMock()
    mock_search.return_value.cost_dollars = 0.05

    with patch("libs.exa.companies.search", side_effect=mock_search) as mock_fn:
        find_companies("test", num_results=20)

        search_input = mock_fn.call_args[0][0]
        assert search_input.num_results == 20


def test_find_companies_include_highlights():
    """Test that find_companies sets highlights when include_highlights=True."""
    mock_search = MagicMock()
    mock_search.return_value.cost_dollars = 0.05

    with patch("libs.exa.companies.search", side_effect=mock_search) as mock_fn:
        find_companies("test", include_highlights=True)

        search_input = mock_fn.call_args[0][0]
        assert search_input.contents is not None
        # Should have highlights in contents
        assert hasattr(search_input.contents, "highlights") or "highlights" in str(
            search_input.contents,
        )


def test_find_companies_output_schema():
    """Test that find_companies accepts output_schema."""
    mock_search = MagicMock()
    mock_search.return_value.cost_dollars = 0.05

    schema = {"type": "object", "properties": {"domain": {"type": "string"}}}

    with patch("libs.exa.companies.search", side_effect=mock_search) as mock_fn:
        find_companies("test", output_schema=schema)

        search_input = mock_fn.call_args[0][0]
        assert search_input.output_schema == schema


def test_find_people_pins_category():
    """Test that find_people pins category='people'."""
    mock_search = MagicMock()
    mock_search.return_value.cost_dollars = 0.05

    with patch("libs.exa.people.search", side_effect=mock_search) as mock_fn:
        find_people("CEO of Anthropic")

        # Verify search was called once
        mock_fn.assert_called_once()
        # Get the SearchInput that was passed
        call_args = mock_fn.call_args
        search_input = call_args[0][0]
        assert search_input.category == "people"
        assert search_input.query == "CEO of Anthropic"


def test_find_people_default_num_results():
    """Test that find_people uses num_results=5 by default."""
    mock_search = MagicMock()
    mock_search.return_value.cost_dollars = 0.05

    with patch("libs.exa.people.search", side_effect=mock_search) as mock_fn:
        find_people("test")

        search_input = mock_fn.call_args[0][0]
        assert search_input.num_results == 5


def test_find_people_custom_num_results():
    """Test that find_people respects custom num_results."""
    mock_search = MagicMock()
    mock_search.return_value.cost_dollars = 0.05

    with patch("libs.exa.people.search", side_effect=mock_search) as mock_fn:
        find_people("test", num_results=15)

        search_input = mock_fn.call_args[0][0]
        assert search_input.num_results == 15
