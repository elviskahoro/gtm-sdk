"""Test Cal.com webhook gate and operations against real fixture shapes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from libs.meetings import canonical_meeting_uid
from src.attio.ops import UpsertMeeting
from src.caldotcom.webhook.booking import Webhook

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]


def _load(fixture_path: str) -> Webhook:
    """Load and validate a fixture."""
    payload = orjson.loads((_REPO_ROOT / fixture_path).read_bytes())
    return Webhook.model_validate(payload)


class TestGateSweepAcrossFixtures:
    """Parametrized gate tests across all real and synthetic fixtures."""

    @pytest.mark.parametrize(
        ("fixture", "expected_valid"),
        [
            ("api/samples/caldotcom.booking.created.redacted.json", True),
            ("api/samples/caldotcom.booking.cancelled.redacted.json", True),
            ("api/samples/caldotcom.booking.rescheduled.redacted.json", True),
            ("api/samples/caldotcom.meeting.started.redacted.json", True),
            ("api/samples/caldotcom.meeting.ended.redacted.json", True),
            ("api/samples/caldotcom.booking.no_show_updated.redacted.json", False),
            ("api/samples/caldotcom.ping.redacted.json", False),
        ],
    )
    def test_attio_is_valid_webhook(
        self,
        fixture: str,
        expected_valid: bool,
    ) -> None:
        """Gate should accept all real booking/meeting fixtures except no_show_updated and ping."""
        w = _load(fixture)
        assert w.attio_is_valid_webhook() is expected_valid


class TestOperationsBuildingForRealShapes:
    """Operations building for real booking and meeting shapes."""

    @pytest.mark.parametrize(
        "fixture",
        [
            "api/samples/caldotcom.booking.created.redacted.json",
            "api/samples/caldotcom.booking.cancelled.redacted.json",
            "api/samples/caldotcom.booking.rescheduled.redacted.json",
            "api/samples/caldotcom.meeting.started.redacted.json",
            "api/samples/caldotcom.meeting.ended.redacted.json",
        ],
    )
    def test_attio_get_operations_returns_upsert_meeting(
        self,
        fixture: str,
    ) -> None:
        """Real shapes should produce one UpsertMeeting operation."""
        w = _load(fixture)
        ops = w.attio_get_operations()

        assert len(ops) == 1
        assert isinstance(ops[0], UpsertMeeting)

    @pytest.mark.parametrize(
        ("fixture", "expected_host_email"),
        [
            ("api/samples/caldotcom.booking.created.redacted.json", "host@dlthub.com"),
            (
                "api/samples/caldotcom.booking.cancelled.redacted.json",
                "alex@example.com",
            ),
            (
                "api/samples/caldotcom.booking.rescheduled.redacted.json",
                "alex@example.com",
            ),
        ],
    )
    def test_attio_get_operations_extracts_host_email_from_correct_field(
        self,
        fixture: str,
        expected_host_email: str,
    ) -> None:
        """Real shapes should extract host email from organizer/user fields."""
        w = _load(fixture)
        ops = w.attio_get_operations()
        op = ops[0]

        # The ical_uid should contain the canonical meeting uid which requires host email
        # If host email was not found, it would fall back to caldotcom-booking-{uid}
        # We can verify host email was extracted by checking it's in the participants
        organizers = [p for p in op.participants if p.is_organizer]
        assert len(organizers) > 0
        assert expected_host_email.lower() in [
            p.email_address.lower() for p in organizers
        ]


class TestCanonicalUidContract:
    """Verify ical_uid equals canonical_meeting_uid(host_email, start) for Fathom join."""

    def test_booking_rescheduled_canonical_uid(self) -> None:
        """booking.rescheduled: host alex@example.com, startTime 2026-05-14T10:00:00Z."""
        w = _load("api/samples/caldotcom.booking.rescheduled.redacted.json")
        ops = w.attio_get_operations()
        op = ops[0]

        # Expected canonical UID
        expected_ical_uid = canonical_meeting_uid(
            host_email="alex@example.com",
            start=datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC),
        )

        assert op.external_ref.ical_uid == expected_ical_uid

    def test_booking_created_canonical_uid(self) -> None:
        """booking.created: host host@dlthub.com, start 2026-05-20T15:00:00Z."""
        w = _load("api/samples/caldotcom.booking.created.redacted.json")
        ops = w.attio_get_operations()
        op = ops[0]

        expected_ical_uid = canonical_meeting_uid(
            host_email="host@dlthub.com",
            start=datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC),
        )

        assert op.external_ref.ical_uid == expected_ical_uid


class TestOrganizerFallbackEmit:
    """Test organizer-only fallback participant emit for bookings without hosts array."""

    def test_booking_cancelled_emits_organizer_fallback(self) -> None:
        """booking.cancelled has no hosts array, should emit organizer as fallback."""
        w = _load("api/samples/caldotcom.booking.cancelled.redacted.json")
        ops = w.attio_get_operations()
        op = ops[0]

        # Should have at least one organizer
        organizers = [p for p in op.participants if p.is_organizer]
        assert len(organizers) >= 1

        # The fallback organizer should be alex@example.com from payload.organizer
        fallback_organizer = next(
            (p for p in organizers if p.email_address == "alex@example.com"),
            None,
        )
        assert fallback_organizer is not None
        assert fallback_organizer.is_organizer is True

    def test_booking_cancelled_organizer_status_maps_to_declined(self) -> None:
        """booking.cancelled with status=CANCELLED should map organizer status to declined."""
        w = _load("api/samples/caldotcom.booking.cancelled.redacted.json")
        ops = w.attio_get_operations()
        op = ops[0]

        # The organizer from the fallback path should have status "declined"
        # because payload.status = "CANCELLED"
        organizers = [p for p in op.participants if p.is_organizer]
        assert len(organizers) >= 1

        # At least one organizer should have "declined" status
        # (from the fallback emit which uses status-driven mapping)
        assert any(org.status == "declined" for org in organizers)


class TestStartEndExtraction:
    """Test that start/end times are correctly extracted from real shapes."""

    @pytest.mark.parametrize(
        ("fixture", "expected_start", "expected_end"),
        [
            (
                "api/samples/caldotcom.booking.rescheduled.redacted.json",
                datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC),
                datetime(2026, 5, 14, 10, 30, 0, tzinfo=UTC),
            ),
            (
                "api/samples/caldotcom.booking.cancelled.redacted.json",
                datetime(2026, 5, 6, 10, 30, 0, tzinfo=UTC),
                datetime(2026, 5, 6, 11, 0, 0, tzinfo=UTC),
            ),
        ],
    )
    def test_start_end_datetime_extraction(
        self,
        fixture: str,
        expected_start: datetime,
        expected_end: datetime,
    ) -> None:
        """Real shapes use startTime/endTime, should be extracted to UpsertMeeting.start/end."""
        w = _load(fixture)
        ops = w.attio_get_operations()
        op = ops[0]

        assert op.start == expected_start
        assert op.end == expected_end

    def test_start_end_are_datetime_not_string(self) -> None:
        """UpsertMeeting.start and .end should be datetime, not raw strings."""
        w = _load("api/samples/caldotcom.booking.rescheduled.redacted.json")
        ops = w.attio_get_operations()
        op = ops[0]

        assert isinstance(op.start, datetime)
        assert isinstance(op.end, datetime)


class TestInvalidFixtures:
    """Test that invalid fixtures correctly report missing fields."""

    def test_no_show_updated_missing_start_and_host_email(self) -> None:
        """no_show_updated is missing startTime and host email fields."""
        w = _load("api/samples/caldotcom.booking.no_show_updated.redacted.json")

        assert w.attio_is_valid_webhook() is False

        error_msg = w.attio_get_invalid_webhook_error_msg()
        assert "start" in error_msg.lower() or "starttime" in error_msg.lower()
        assert "host" in error_msg.lower() or "email" in error_msg.lower()

    def test_ping_missing_uid(self) -> None:
        """PING webhook has no uid, should fail gate."""
        w = _load("api/samples/caldotcom.ping.redacted.json")
        assert w.attio_is_valid_webhook() is False
