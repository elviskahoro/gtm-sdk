"""Webhook ETL contract for Cal.com booking ingestion."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from uuid_extensions import uuid7

from libs.caldotcom import Webhook as CalcomWebhook
from libs.dlt.bucket_naming import etl_bucket_name, raw_bucket_name
from libs.meetings import canonical_meeting_uid
from src.caldotcom.utils import (
    generate_gcs_filename,
    webhook_to_jsonl,
)

# Cal.com Booking has a single top-level RSVP status that applies to the
# booking as a whole, plus a per-attendee `absent` boolean. Map to Attio's
# four-value enum: accepted | tentative | declined | pending.
_CALCOM_BOOKING_STATUS_TO_ATTIO: dict[
    str,
    Literal["accepted", "tentative", "declined", "pending"],
] = {
    "accepted": "accepted",
    "pending": "pending",
    "cancelled": "declined",
    "rejected": "declined",
}


def _caldotcom_status_to_attio(
    booking_status: str | None,
    *,
    absent: bool = False,
) -> Literal["accepted", "tentative", "declined", "pending"]:
    if absent:
        return "declined"
    if booking_status is None:
        return "accepted"
    normalized = booking_status.lower()
    return _CALCOM_BOOKING_STATUS_TO_ATTIO.get(normalized, "accepted")


def _first_host_email(payload: dict[str, Any]) -> str | None:
    for h in payload.get("hosts") or []:
        email = h.get("email")
        if email:
            return str(email)
    organizer = payload.get("organizer") or {}
    if organizer.get("email"):
        return str(organizer["email"])
    user = payload.get("user") or {}
    if user.get("email"):
        return str(user["email"])
    primary = payload.get("userPrimaryEmail")
    if primary:
        return str(primary)
    return None


def _parse_start(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _start_value(payload: dict[str, Any]) -> Any:
    """Raw start value preserving original type (str or datetime) for downstream consumers."""
    return payload.get("start") or payload.get("startTime")


def _end_value(payload: dict[str, Any]) -> Any:
    return payload.get("end") or payload.get("endTime")


class Webhook(CalcomWebhook):
    """Webhook subclass implementing ETL contract for Cal.com bookings."""

    @staticmethod
    def modal_get_secret_collection_names() -> list[str]:
        return ["devx-gcp-202605111323"]

    @staticmethod
    def etl_get_bucket_name() -> str:
        return etl_bucket_name(source="calcom", entity_plural="bookings")

    @staticmethod
    def raw_get_bucket_name() -> str:
        return raw_bucket_name(source="calcom", entity_plural="bookings")

    @staticmethod
    def raw_get_app_name() -> str:
        from libs.dlt.filesystem_gcp import CloudGoogle

        return CloudGoogle.clean_bucket_name(bucket_name=Webhook.raw_get_bucket_name())

    # Raw passthrough has no per-source invariants — it lands the JSON body
    # untouched. Trivial implementations satisfy the symmetric-triple contract
    # the registry sync expects without inventing fake validity rules.
    def raw_is_valid_webhook(self) -> bool:
        return True

    def raw_get_invalid_webhook_error_msg(self) -> str:
        return "raw passthrough accepts any payload; should not be reachable"

    @staticmethod
    def storage_get_app_name() -> str:
        return Webhook.etl_get_bucket_name()

    @staticmethod
    def storage_get_base_model_type() -> type[BaseModel] | None:
        return None

    @staticmethod
    def lance_get_project_name() -> str:
        raise NotImplementedError("LanceDB integration is Phase 2+")

    @staticmethod
    def lance_get_base_model_type() -> str:
        raise NotImplementedError("LanceDB integration is Phase 2+")

    def etl_is_valid_webhook(self) -> bool:
        return True

    def etl_get_invalid_webhook_error_msg(self) -> str:
        return "This webhook family does not support ETL output"

    def _booking_id(self) -> str:
        # Booking events nest the booking under `payload`; meeting events were
        # normalized into the same shape by the model validator. uid is the
        # stable string id; fall back to numeric ids, then uuid7 for PING.
        # Cached on the instance so filename and JSONL rows agree when we
        # have to synthesize a uuid.
        cached = getattr(self, "_cached_booking_id", None)
        if cached is not None:
            return cached
        payload = self.payload or {}
        uid = payload.get("uid") or payload.get("bookingUid")
        if not uid:
            for key in ("bookingId", "id"):
                if key in payload and payload[key] is not None:
                    uid = payload[key]
                    break
        if not uid:
            uid = uuid7()
        booking_id = str(uid)
        object.__setattr__(self, "_cached_booking_id", booking_id)
        return booking_id

    def etl_get_json(self, storage: Any = None) -> str:
        return webhook_to_jsonl(self.model_dump(mode="json"), self._booking_id())

    def etl_get_file_name(self) -> str:
        return generate_gcs_filename(
            self.createdAt,
            self.triggerEvent,
            self._booking_id(),
        )

    def etl_get_base_models(self, storage: Any) -> list[Any]:
        raise NotImplementedError("LanceDB integration is Phase 2+")

    # --- Attio export contract ---

    @staticmethod
    def attio_get_secret_collection_names() -> list[str]:
        return ["attio"]

    @staticmethod
    def attio_get_app_name() -> str:
        return "export-to-attio-from-calcom-bookings"

    def attio_is_valid_webhook(self) -> bool:
        payload = self.payload or {}
        uid = payload.get("uid") or payload.get("bookingUid")
        attendees = payload.get("attendees") or []
        host_email = _first_host_email(payload)
        start_dt = _parse_start(_start_value(payload))
        end_dt = _parse_start(_end_value(payload))
        return (
            bool(uid)
            and bool(attendees)
            and bool(host_email)
            and isinstance(start_dt, datetime)
            and isinstance(end_dt, datetime)
        )

    def attio_get_invalid_webhook_error_msg(self) -> str:
        return "Cal.com booking missing one of: uid/bookingUid, attendees, host email (hosts/organizer/user/userPrimaryEmail), start/startTime, or end/endTime (or invalid timestamp format)"

    def attio_get_operations(self) -> list[Any]:
        from src.attio.ops import (
            MeetingExternalRef,
            MeetingParticipant,
            UpsertMeeting,
        )

        payload = self.payload or {}
        uid = str(payload.get("uid") or payload.get("bookingUid") or "")
        host_email = _first_host_email(payload)
        # Gate ensures start/end values exist; _parse_start normalizes them to datetime
        start_dt: datetime | None = _parse_start(_start_value(payload))
        end_dt: datetime | None = _parse_start(_end_value(payload))
        # Shared synthetic ical_uid lets Fathom's webhook write to the same Attio
        # record for the same meeting. Cal.com's icsUid is ignored on purpose:
        # Fathom does not see it, so using it would re-introduce duplicates.
        if host_email and start_dt is not None:
            ical_uid = canonical_meeting_uid(host_email=host_email, start=start_dt)
        else:
            ical_uid = f"caldotcom-booking-{uid}"

        title = payload.get("title") or "Cal.com booking"
        description = (
            payload.get("additionalNotes") or payload.get("description") or title
        )
        booking_status = payload.get("status")

        attendees: list[MeetingParticipant] = []
        for a in payload.get("attendees") or []:
            email = a.get("email")
            if not email:
                continue
            attendees.append(
                MeetingParticipant(
                    email_address=email,
                    is_organizer=False,
                    status=_caldotcom_status_to_attio(
                        booking_status,
                        absent=bool(a.get("absent")),
                    ),
                ),
            )

        for h in payload.get("hosts") or []:
            email = h.get("email")
            if not email:
                continue
            attendees.append(
                MeetingParticipant(
                    email_address=email,
                    is_organizer=True,
                    status="accepted",
                ),
            )

        if not (payload.get("hosts") or []):
            fallback_email: str | None = None
            for source_key in ("organizer", "user"):
                candidate = payload.get(source_key) or {}
                email = candidate.get("email")
                if email:
                    fallback_email = str(email)
                    break
            if fallback_email is None:
                primary = payload.get("userPrimaryEmail")
                if primary:
                    fallback_email = str(primary)
            if fallback_email is not None:
                attendees.append(
                    MeetingParticipant(
                        email_address=fallback_email,
                        is_organizer=True,
                        status=_caldotcom_status_to_attio(booking_status),
                    ),
                )

        # Gate check ensures start_dt/end_dt are datetime; assert for type checker
        assert isinstance(start_dt, datetime), (
            "start must be datetime after gate validation"
        )
        assert isinstance(end_dt, datetime), (
            "end must be datetime after gate validation"
        )

        return [
            UpsertMeeting(
                external_ref=MeetingExternalRef(
                    ical_uid=ical_uid,
                    provider="google",
                    is_recurring=False,
                ),
                title=title,
                description=description,
                start=start_dt,
                end=end_dt,
                is_all_day=False,
                participants=attendees,
            ),
        ]
