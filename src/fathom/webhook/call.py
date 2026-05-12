"""Webhook ETL contract for Fathom recording ingestion."""

from typing import Any

from pydantic import BaseModel

from libs.fathom import Webhook as FathomWebhook
from src.fathom.utils import (
    generate_gcs_filename,
    recording_to_jsonl,
)


class Webhook(FathomWebhook):
    """Webhook subclass implementing ETL contract for Fathom recordings."""

    @staticmethod
    def modal_get_secret_collection_names() -> list[str]:
        return ["devx-growth-gcp"]

    @staticmethod
    def etl_get_bucket_name() -> str:
        return "devx-fathom-recording-etl"

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

    def etl_get_json(self, storage: Any = None) -> str:
        return recording_to_jsonl(self.model_dump(mode="json"), self.recording_id)

    def etl_get_file_name(self) -> str:
        return generate_gcs_filename(
            self.recording_start_time,
            self.recording_id,
            self.meeting_title or self.title,
        )

    def etl_get_base_models(self, storage: Any) -> list[Any]:
        raise NotImplementedError("LanceDB integration is Phase 2+")

    # --- Attio export contract ---

    @staticmethod
    def attio_get_secret_collection_names() -> list[str]:
        return ["attio"]

    def attio_is_valid_webhook(self) -> bool:
        return bool(self.recording_id) and bool(self.calendar_invitees)

    def attio_get_invalid_webhook_error_msg(self) -> str:
        return (
            "Fathom call payload is not exportable to Attio "
            "(no attendees or recording_id)"
        )

    def attio_get_operations(self) -> list[Any]:
        from src.attio.ops import (
            MeetingExternalRef,
            MeetingParticipant,
            UpsertMeeting,
        )

        description: str = (
            self.default_summary.markdown_formatted
            if self.default_summary
            else (self.meeting_title or self.title)
        )
        return [
            UpsertMeeting(
                external_ref=MeetingExternalRef(
                    ical_uid=f"fathom-call-{self.recording_id}",
                    provider="google",
                    is_recurring=False,
                ),
                title=self.meeting_title or self.title,
                description=description,
                start=self.scheduled_start_time,
                end=self.scheduled_end_time,
                is_all_day=False,
                participants=[
                    MeetingParticipant(
                        email_address=inv.email,
                        is_organizer=(inv.email == self.recorded_by.email),
                    )
                    for inv in self.calendar_invitees
                ],
            ),
        ]
