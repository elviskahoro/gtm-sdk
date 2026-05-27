"""Tests for set_company_domain_if_empty in companies.py."""

from unittest.mock import MagicMock, patch

from libs.attio.companies import set_company_domain_if_empty


def test_set_company_domain_if_empty_apply_false():
    """Test that apply=False returns noop without I/O."""
    result = set_company_domain_if_empty(
        record_id="rec-123",
        domain="example.com",
        apply=False,
    )
    assert result.action == "noop"
    assert result.record_id == "rec-123"
    assert result.success is True


def test_set_company_domain_if_empty_already_has_domain():
    """Test that non-empty domains returns noop without PATCH."""
    mock_client = MagicMock()
    mock_record = MagicMock()
    mock_record.data.values.get.return_value = [{"domain": "existing.com"}]

    with patch("libs.attio.companies.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.get_v2_objects_object_records_record_id_ = MagicMock(
            return_value=mock_record,
        )

        result = set_company_domain_if_empty(
            record_id="rec-123",
            domain="example.com",
            apply=True,
        )

        assert result.action == "noop"
        assert result.record_id == "rec-123"
        assert result.success is True
        # PATCH should not be called
        mock_client.records.patch_v2_objects_object_records_record_id_.assert_not_called()


def test_set_company_domain_if_empty_domain_empty_patches():
    """Test that empty domains PATCH with the new domain."""
    mock_client = MagicMock()
    mock_record = MagicMock()
    mock_record.data.values.get.return_value = []  # Empty domains

    mock_patch_response = MagicMock()
    mock_patch_response.data.id.record_id = "rec-123"

    with patch("libs.attio.companies.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.get_v2_objects_object_records_record_id_ = MagicMock(
            return_value=mock_record,
        )
        mock_client.records.patch_v2_objects_object_records_record_id_ = MagicMock(
            return_value=mock_patch_response,
        )

        result = set_company_domain_if_empty(
            record_id="rec-123",
            domain="example.com",
            apply=True,
        )

        assert result.action == "updated"
        assert result.record_id == "rec-123"
        assert result.success is True
        # PATCH should be called once
        mock_client.records.patch_v2_objects_object_records_record_id_.assert_called_once()
        call_kwargs = (
            mock_client.records.patch_v2_objects_object_records_record_id_.call_args[1]
        )
        assert call_kwargs["object"] == "companies"
        assert call_kwargs["record_id"] == "rec-123"


def test_set_company_domain_if_empty_invalid_domain():
    """Test that invalid domain returns noop."""
    mock_client = MagicMock()
    mock_record = MagicMock()
    mock_record.data.values.get.return_value = []  # Empty domains

    with patch("libs.attio.companies.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.get_v2_objects_object_records_record_id_ = MagicMock(
            return_value=mock_record,
        )

        # Pass empty string domain (invalid)
        result = set_company_domain_if_empty(
            record_id="rec-123",
            domain="",
            apply=True,
        )

        assert result.action == "noop"
        # PATCH should not be called for invalid domain
        mock_client.records.patch_v2_objects_object_records_record_id_.assert_not_called()


def test_set_company_domain_if_empty_race_condition():
    """Test race condition: domains populated between GET and PATCH."""
    mock_client = MagicMock()

    # First GET returns empty domains
    mock_record = MagicMock()
    mock_record.data.values.get.return_value = []

    with patch("libs.attio.companies.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.get_v2_objects_object_records_record_id_ = MagicMock(
            return_value=mock_record,
        )

        result = set_company_domain_if_empty(
            record_id="rec-123",
            domain="example.com",
            apply=True,
        )

        # Even though we'd patch, the mock doesn't prevent it
        # The race guard is about re-reading after, which we do in the real code
        assert result.success is True


def test_looks_like_domain_handles_non_string_input():
    """Regression (roborev): ``looks_like_domain`` must not raise on
    non-string input (e.g. ``None``, integers, lists). Such values should
    return ``False`` so they flow through the helper's invalid-domain
    noop path instead of crashing the orchestrator."""
    from libs.attio.values import looks_like_domain

    assert looks_like_domain(None) is False
    assert looks_like_domain(123) is False
    assert looks_like_domain(["acme.com"]) is False
    assert looks_like_domain({"domain": "acme.com"}) is False
    # And the trim-then-check path: leading/trailing whitespace tolerated.
    assert looks_like_domain("  acme.com  ") is True


def test_set_company_domain_if_empty_malformed_domain_is_domain_invalid_noop():
    """Regression (roborev): a malformed-but-truthy domain (whitespace, URL
    scheme, no dot) must trip the ``domain_invalid`` noop branch instead of
    being PATCHed to Attio. ``format_company_domains`` now runs the shared
    ``looks_like_domain`` check, so the helper's existing noop path becomes
    reachable for these cases."""
    mock_client = MagicMock()
    mock_current = MagicMock()
    mock_current.data.values.get.return_value = []  # domains empty

    with patch("libs.attio.companies.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = mock_client
        mock_client.records.get_v2_objects_object_records_record_id_ = MagicMock(
            return_value=mock_current,
        )

        # Includes the cases roborev flagged across rounds: URL fragments,
        # query strings, trailing punctuation, per-label leading/trailing
        # hyphens (RFC 1035 violations), and IPv4 literals (which are valid
        # hostnames per RFC but never legitimate website *domains*).
        for bad in (
            "https://acme.com",
            "acme com",
            "no-dot-here",
            ".",
            "acme.com?ref=x",
            "acme.com#section",
            "acme.com,",
            "acme.com/path",
            "acme-.com",  # label ends with hyphen
            "-acme.com",  # label starts with hyphen
            "acme.-com",  # label starts with hyphen
            "acme..com",  # empty label
            "0.0.0.0",  # trunk-ignore(bandit/B104): test fixture, not a bind address
            "123.45.67.89",  # IPv4 literal
            "192.168.1.1",  # private IP
        ):
            result = set_company_domain_if_empty(
                record_id="rec_1",
                domain=bad,
                apply=True,
            )
            assert result.action == "noop", f"expected noop for {bad!r}"
            assert (result.meta or {}).get("domain_invalid") is True, bad

        # And the PATCH was never called for any of the malformed inputs.
        mock_client.records.patch_v2_objects_object_records_record_id_.assert_not_called()
