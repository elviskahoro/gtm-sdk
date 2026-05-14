from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


def _valid_kwargs() -> dict[str, object]:
    return dict(
        external_id="rb2b:abc123",
        name="https://example.test/pricing",
        event_type="rb2b_visit",
        event_timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
        body_json='{"raw": "payload"}',
        captured_url="https://example.test/pricing",
    )


def test_tracking_event_input_minimal() -> None:
    from libs.attio.models import TrackingEventInput

    i = TrackingEventInput(**_valid_kwargs())
    assert i.related_person_record_id is None
    assert i.related_company_record_id is None
    assert i.tags == []


def test_tracking_event_input_with_resolved_refs() -> None:
    from libs.attio.models import TrackingEventInput

    i = TrackingEventInput(
        **_valid_kwargs(),
        related_person_record_id="pe_123",
        related_company_record_id="co_456",
    )
    assert i.related_person_record_id == "pe_123"
    assert i.related_company_record_id == "co_456"


def test_tracking_event_input_forbids_extra() -> None:
    from libs.attio.models import TrackingEventInput

    with pytest.raises(ValidationError):
        TrackingEventInput(**_valid_kwargs(), bogus="x")
