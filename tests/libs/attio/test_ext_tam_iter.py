"""Tests for iter_company_ids_by_filter in ext_tam.py."""

from unittest.mock import MagicMock, patch

from libs.attio.ext_tam import iter_company_ids_by_filter


def test_iter_company_ids_by_filter_single_page():
    """Test iteration over a single page of results."""
    mock_client = MagicMock()

    # Mock records with accounts
    mock_record1 = MagicMock()
    mock_record1.values.get.return_value = [
        {"target_record_id": "company-1"},
    ]

    mock_record2 = MagicMock()
    mock_record2.values.get.return_value = [
        {"target_record_id": "company-2"},
    ]

    mock_response = MagicMock()
    mock_response.data = [mock_record1, mock_record2]

    with patch("libs.attio.ext_tam.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.post_v2_objects_object_records_query = MagicMock(
            return_value=mock_response,
        )

        filter_dict = {"source": "snowflake_scored_accounts_csv"}
        result = list(iter_company_ids_by_filter(filter_dict))

        assert result == ["company-1", "company-2"]
        # Verify the query was called correctly
        call_kwargs = (
            mock_client.records.post_v2_objects_object_records_query.call_args[1]
        )
        assert call_kwargs["object"] == "ext_tam"
        assert call_kwargs["filter_"] == filter_dict
        assert call_kwargs["limit"] == 100
        assert call_kwargs["offset"] == 0


def test_iter_company_ids_by_filter_rejects_invalid_page_size():
    """Regression (roborev): ``page_size <= 0`` would prevent offset from
    advancing and loop forever. ``> 100`` exceeds Attio's per-query cap.
    Both must be rejected with a clear ValueError."""
    import pytest

    with pytest.raises(ValueError, match="page_size"):
        list(iter_company_ids_by_filter({"source": "x"}, page_size=0))
    with pytest.raises(ValueError, match="page_size"):
        list(iter_company_ids_by_filter({"source": "x"}, page_size=-1))
    with pytest.raises(ValueError, match="page_size"):
        list(iter_company_ids_by_filter({"source": "x"}, page_size=101))


def test_iter_company_ids_by_filter_with_attribute_shaped_accounts():
    """Regression (roborev): Attio SDK returns relationship values as objects
    with attributes, not dicts. The iterator must read ``target_record_id``
    via attribute access, not ``.get()``."""

    class _AccountObj:
        def __init__(self, rid: str):
            self.target_record_id = rid

    mock_client = MagicMock()
    mock_record = MagicMock()
    mock_record.values.get.return_value = [_AccountObj("company-attr-1")]
    mock_response = MagicMock()
    mock_response.data = [mock_record]

    with patch("libs.attio.ext_tam.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.post_v2_objects_object_records_query = MagicMock(
            return_value=mock_response,
        )

        result = list(iter_company_ids_by_filter({"source": "x"}))

    assert result == ["company-attr-1"]


def test_iter_company_ids_by_filter_multiple_pages():
    """Test iteration over multiple pages."""
    mock_client = MagicMock()

    # First page
    mock_record1 = MagicMock()
    mock_record1.values.get.return_value = [{"target_record_id": "company-1"}]

    mock_response1 = MagicMock()
    mock_response1.data = [mock_record1] * 100  # Full page

    # Second page
    mock_record2 = MagicMock()
    mock_record2.values.get.return_value = [{"target_record_id": "company-2"}]

    mock_response2 = MagicMock()
    mock_response2.data = [mock_record2] * 50  # Partial page (stops iteration)

    with patch("libs.attio.ext_tam.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        # Alternate responses between pages
        mock_client.records.post_v2_objects_object_records_query.side_effect = [
            mock_response1,
            mock_response2,
        ]

        filter_dict = {}
        result = list(iter_company_ids_by_filter(filter_dict))

        # Should have company-1 (100 times deduplicated to 1) + company-2 (50 times to 1)
        assert len(result) == 2
        assert result[0] == "company-1"
        assert result[1] == "company-2"
        # Should have called the query twice
        assert mock_client.records.post_v2_objects_object_records_query.call_count == 2


def test_iter_company_ids_by_filter_deduplicates_across_pages():
    """Test that duplicate company IDs across pages are deduplicated."""
    mock_client = MagicMock()

    # First page
    mock_record1 = MagicMock()
    mock_record1.values.get.return_value = [{"target_record_id": "company-1"}]

    mock_response1 = MagicMock()
    mock_response1.data = [mock_record1]

    # Second page with same company
    mock_record2 = MagicMock()
    mock_record2.values.get.return_value = [{"target_record_id": "company-1"}]

    mock_response2 = MagicMock()
    mock_response2.data = [mock_record2]

    with patch("libs.attio.ext_tam.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.post_v2_objects_object_records_query.side_effect = [
            mock_response1,
            mock_response2,
        ]

        result = list(iter_company_ids_by_filter({}))

        # Should only yield company-1 once despite two records
        assert result == ["company-1"]


def test_iter_company_ids_by_filter_skips_missing_accounts():
    """Test that records without accounts are skipped."""
    mock_client = MagicMock()

    # Record with accounts
    mock_record1 = MagicMock()
    mock_record1.values.get.return_value = [{"target_record_id": "company-1"}]

    # Record without accounts
    mock_record2 = MagicMock()
    mock_record2.values.get.return_value = []  # Empty accounts

    # Record with None target_record_id
    mock_record3 = MagicMock()
    mock_record3.values.get.return_value = [{"target_record_id": None}]

    mock_response = MagicMock()
    mock_response.data = [mock_record1, mock_record2, mock_record3]

    with patch("libs.attio.ext_tam.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.post_v2_objects_object_records_query = MagicMock(
            return_value=mock_response,
        )

        result = list(iter_company_ids_by_filter({}))

        # Should only yield company-1, skipping the others
        assert result == ["company-1"]


def test_iter_company_ids_by_filter_empty_result():
    """Test iteration over empty results."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.data = []

    with patch("libs.attio.ext_tam.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.post_v2_objects_object_records_query = MagicMock(
            return_value=mock_response,
        )

        result = list(iter_company_ids_by_filter({"source": "nonexistent"}))

        assert result == []


def test_iter_company_ids_by_filter_custom_page_size():
    """Test custom page size parameter."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.data = []

    with patch("libs.attio.ext_tam.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.post_v2_objects_object_records_query = MagicMock(
            return_value=mock_response,
        )

        list(iter_company_ids_by_filter({}, page_size=50))

        # Verify page_size was passed
        call_kwargs = (
            mock_client.records.post_v2_objects_object_records_query.call_args[1]
        )
        assert call_kwargs["limit"] == 50


def test_iter_company_ids_by_filter_compound_filter():
    """Test iteration with compound $and filter."""
    mock_client = MagicMock()
    mock_record = MagicMock()
    mock_record.values.get.return_value = [{"target_record_id": "company-1"}]

    mock_response = MagicMock()
    mock_response.data = [mock_record]

    with patch("libs.attio.ext_tam.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.post_v2_objects_object_records_query = MagicMock(
            return_value=mock_response,
        )

        filter_dict = {
            "$and": [
                {"source": "snowflake_scored_accounts_csv"},
                {"source_snapshot_date": "2025-05-27"},
            ],
        }
        list(iter_company_ids_by_filter(filter_dict))

        # Verify filter was passed verbatim
        call_kwargs = (
            mock_client.records.post_v2_objects_object_records_query.call_args[1]
        )
        assert call_kwargs["filter_"] == filter_dict
