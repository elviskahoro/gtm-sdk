from __future__ import annotations

from unittest.mock import MagicMock, patch


def _person(record_id: str, full_name: str, company_id: str | None = None) -> MagicMock:
    rec = MagicMock()
    rec.id.record_id = record_id
    name_value = MagicMock()
    name_value.full_name = full_name
    values: dict[str, list[MagicMock]] = {"name": [name_value]}
    if company_id is not None:
        company_value = MagicMock()
        company_value.target_record_id = company_id
        values["company"] = [company_value]
    rec.values = values
    return rec


def test_find_person_by_name_at_company_single_match() -> None:
    fake_client = MagicMock()
    fake_client.records.post_v2_objects_object_records_query.return_value.data = [
        _person("rec-1", "Jane Doe"),
    ]
    with patch("libs.attio.people.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.people import find_person_by_name_at_company

        out = find_person_by_name_at_company("Jane Doe", "cid")
    assert out == "rec-1"


def test_find_person_by_name_at_company_prefers_linked_match_on_multi() -> None:
    fake_client = MagicMock()
    fake_client.records.post_v2_objects_object_records_query.return_value.data = [
        _person("rec-1", "Jane Doe", company_id="other-cid"),
        _person("rec-2", "Jane Doe", company_id="cid"),
        _person("rec-3", "Jane Doe", company_id=None),
    ]
    with patch("libs.attio.people.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.people import find_person_by_name_at_company

        out = find_person_by_name_at_company("Jane Doe", "cid")
    assert out == "rec-2"


def test_find_person_by_name_at_company_single_word_name() -> None:
    fake_client = MagicMock()
    fake_client.records.post_v2_objects_object_records_query.return_value.data = [
        _person("rec-1", "Carole"),
    ]
    with patch("libs.attio.people.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.people import find_person_by_name_at_company

        out = find_person_by_name_at_company("Carole", "cid")
    assert out == "rec-1"


def test_find_person_by_name_at_company_none_on_miss() -> None:
    fake_client = MagicMock()
    fake_client.records.post_v2_objects_object_records_query.return_value.data = []
    with patch("libs.attio.people.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.people import find_person_by_name_at_company

        assert find_person_by_name_at_company("Nobody Else", "cid") is None


def test_stub_create_person_preview() -> None:
    fake_client = MagicMock()
    with patch("libs.attio.people.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.people import stub_create_person

        out = stub_create_person("Jane Doe", "cid", apply=False)
    assert out.startswith("preview-")
    fake_client.records.post_v2_objects_object_records.assert_not_called()
