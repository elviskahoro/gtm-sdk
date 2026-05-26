from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from libs.attio.models import ExtTamInput, NoteInput


def test_ext_tam_input_minimal_roundtrip() -> None:
    instance = ExtTamInput(
        name="Acme (Jane Doe @ Snowflake)",
        person_self_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        employer_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        account_ids=["cccccccc-cccc-cccc-cccc-cccccccccccc"],
        source="snowflake_scored_accounts_csv",
        source_snapshot_date=date(2026, 5, 25),
    )
    dumped = instance.model_dump()
    assert dumped["partner_score"] is None
    assert dumped["internal_score"] is None
    assert dumped["connection_created_date"] is None
    assert dumped["source_snapshot_date"] == date(2026, 5, 25)


def test_ext_tam_input_full() -> None:
    instance = ExtTamInput(
        name="Acme (Jane Doe @ Snowflake)",
        person_self_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        employer_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        account_ids=["cccccccc-cccc-cccc-cccc-cccccccccccc"],
        customer_region="NORTH AMERICA - EAST",
        customer_district="NYC METRO",
        coverage_type="Capacity",
        last_connection_date=date(2026, 5, 18),
        connection_created_date=date(2025, 1, 5),
        partner_score=8.5,
        internal_score=3.0,
        source="snowflake_scored_accounts_csv",
        source_snapshot_date=date(2026, 5, 25),
    )
    assert instance.connection_created_date == date(2025, 1, 5)
    assert instance.partner_score == pytest.approx(8.5)


def test_ext_tam_input_account_ids_required_non_empty() -> None:
    with pytest.raises(ValidationError):
        ExtTamInput(
            name="x",
            person_self_id="x",
            employer_id="y",
            account_ids=[],
            source="snowflake_scored_accounts_csv",
            source_snapshot_date=date(2026, 5, 25),
        )


def test_note_input_accepts_created_at_and_meeting_id() -> None:
    instance = NoteInput(
        title="t",
        content="body",
        parent_object="companies",
        parent_record_id="cid",
        format="markdown",
        created_at=datetime(2025, 1, 5, tzinfo=UTC),
        meeting_id=None,
    )
    assert instance.created_at == datetime(2025, 1, 5, tzinfo=UTC)
    assert instance.meeting_id is None


def test_note_input_defaults_preserve_back_compat() -> None:
    instance = NoteInput(
        title="t",
        content="body",
        parent_object="companies",
        parent_record_id="cid",
    )
    assert instance.created_at is None
    assert instance.meeting_id is None
    assert instance.format == "plaintext"
