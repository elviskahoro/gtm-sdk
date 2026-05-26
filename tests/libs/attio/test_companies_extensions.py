from __future__ import annotations

from unittest.mock import MagicMock, patch


def _query_data(names: list[tuple[str, str]]) -> MagicMock:
    """Build a fake ``post_v2_objects_object_records_query`` response.

    ``names`` is a list of ``(record_id, name)``.
    """
    response = MagicMock()
    fake_records = []
    for record_id, name in names:
        rec = MagicMock()
        rec.id.record_id = record_id
        name_value = MagicMock()
        name_value.value = name
        rec.values = {"name": [name_value]}
        fake_records.append(rec)
    response.data = fake_records
    return response


def test_find_company_by_name_exact_match() -> None:
    fake_client = MagicMock()
    fake_client.records.post_v2_objects_object_records_query.return_value = _query_data(
        [("rec-1", "Snowflake")],
    )
    with patch("libs.attio.companies.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.companies import find_company_by_name

        assert find_company_by_name("Snowflake") == "rec-1"


def test_find_company_by_name_normalized_fallback() -> None:
    fake_client = MagicMock()
    # First call (exact $eq) returns no rows.
    # Second call (broader) returns two rows whose normalized names collide.
    fake_client.records.post_v2_objects_object_records_query.side_effect = [
        _query_data([]),
        _query_data([("rec-2", "Acme, Inc."), ("rec-3", "Acme")]),
    ]
    with patch("libs.attio.companies.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.companies import find_company_by_name

        # Both candidates normalize to "acme"; result is one of them
        # (lexicographically smallest record_id).
        out = find_company_by_name("acme inc")
    assert out in {"rec-2", "rec-3"}
    assert out == "rec-2"  # smallest by lex


def test_find_company_by_name_returns_none_on_miss() -> None:
    fake_client = MagicMock()
    fake_client.records.post_v2_objects_object_records_query.return_value = _query_data(
        [],
    )
    with patch("libs.attio.companies.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.companies import find_company_by_name

        assert find_company_by_name("nonexistent") is None


def test_stub_create_company_preview_no_writes() -> None:
    fake_client = MagicMock()
    with patch("libs.attio.companies.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.companies import stub_create_company

        out = stub_create_company("Acme", apply=False)
    assert out.startswith("preview-")
    fake_client.records.post_v2_objects_object_records.assert_not_called()


def test_set_company_owner_no_change_when_already_set() -> None:
    fake_client = MagicMock()
    # `get_v2_objects_object_records_record_id_` returns a record whose
    # owner_attr value already references `pid-1`.
    existing_record = MagicMock()
    owner_value = MagicMock()
    owner_value.referenced_actor_id = "pid-1"
    existing_record.values = {"owner": [owner_value]}
    fake_client.records.get_v2_objects_object_records_record_id_.return_value.data = (
        existing_record
    )

    with patch("libs.attio.companies.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.companies import set_company_owner

        envelope = set_company_owner(
            company_record_id="cid",
            person_record_id="pid-1",
            apply=True,
        )
    assert envelope.action == "noop"
    fake_client.records.patch_v2_objects_object_records_record_id_.assert_not_called()


def test_set_company_owner_preview_returns_noop() -> None:
    fake_client = MagicMock()
    with patch("libs.attio.companies.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.companies import set_company_owner

        envelope = set_company_owner(
            company_record_id="cid",
            person_record_id="pid",
            apply=False,
        )
    assert envelope.action == "noop"
    fake_client.records.patch_v2_objects_object_records_record_id_.assert_not_called()
