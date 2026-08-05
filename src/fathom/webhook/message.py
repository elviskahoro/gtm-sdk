"""Webhook ETL contract for Fathom transcript-message ingestion.

Fathom delivers a recording as one webhook payload.  The message stream is
the recording transcript, so this handler preserves the recording metadata on
each row while expanding each transcript utterance into its own JSONL row.
"""

from typing import Any

import orjson
from pydantic import BaseModel

from libs.dlt import etl_bucket_name, raw_bucket_name
from libs.fathom import Webhook as FathomWebhook
from src.fathom.utils import generate_gcs_filename, generate_row_id


class Webhook(FathomWebhook):
    """Fathom recording webhook projected into transcript message rows."""

    @staticmethod
    def modal_get_secret_collection_names() -> list[str]:
        return ["devx-gcp-202605260000"]

    @staticmethod
    def etl_get_bucket_name() -> str:
        return etl_bucket_name(source="fathom", entity_plural="messages")

    @staticmethod
    def raw_get_bucket_name() -> str:
        return raw_bucket_name(source="fathom", entity_plural="messages")

    @staticmethod
    def raw_get_app_name() -> str:
        from libs.dlt import CloudGoogle

        return CloudGoogle.clean_bucket_name(bucket_name=Webhook.raw_get_bucket_name())

    # Raw passthrough has no per-source invariants — see caldotcom/booking.py.
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
        raise NotImplementedError("Fathom message ETL is not implemented")

    @staticmethod
    def lance_get_base_model_type() -> str:
        raise NotImplementedError("Fathom message ETL is not implemented")

    def etl_is_valid_webhook(self) -> bool:
        return bool(self.transcript)

    def etl_get_invalid_webhook_error_msg(self) -> str:
        return "Fathom message webhook has no transcript messages"

    def etl_get_json(self, storage: Any = None) -> str:
        del storage
        metadata = self.model_dump(mode="json", exclude={"transcript"})
        lines = []
        for index, message in enumerate(self.transcript or []):
            lines.append(
                orjson.dumps(
                    {
                        **metadata,
                        "id": generate_row_id(self.recording_id, index),
                        "speaker_display_name": message.speaker.display_name,
                        "speaker_matched_calendar_invitee_email": (
                            message.speaker.matched_calendar_invitee_email
                        ),
                        "text": message.text,
                        "timestamp": message.timestamp,
                    },
                ).decode("utf-8"),
            )
        return "\n".join(lines) + "\n"

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
    def required_api_keys() -> list[str]:
        return ["ATTIO_API_KEY"]

    @staticmethod
    def optional_api_keys() -> list[str]:
        return []

    @staticmethod
    def attio_get_app_name() -> str:
        return "export-to-attio-from-fathom-messages"

    # --- Slack export contract (not implemented for this source) ---
    # Present so the WebhookModelProtocol conformance test passes; this source
    # is never wired into webhooks/export_to_slack.py.
    @staticmethod
    def slack_get_app_name() -> str:
        return "export-to-slack-from-fathom-messages"

    @staticmethod
    def slack_get_channel_secret_name() -> str:
        return "UNSUPPORTED_SLACK_CHANNEL_ID"

    def slack_is_valid_webhook(self) -> bool:
        return False

    def slack_get_invalid_webhook_error_msg(self) -> str:
        return "Slack export is not supported for Fathom messages"

    def slack_get_messages(self) -> list[Any]:
        return []

    # --- Clay webhook-table export contract (not implemented for this source) ---

    @staticmethod
    def clay_get_app_name() -> str:
        return "export-to-clay-from-fathom-messages"

    @staticmethod
    def clay_get_webhook_url_secret_name() -> str:
        return "UNSUPPORTED_CLAY_WEBHOOK_URL"

    @staticmethod
    def clay_get_webhook_auth_token_secret_name() -> str:
        return "UNSUPPORTED_CLAY_WEBHOOK_AUTH_TOKEN"

    def clay_is_valid_webhook(self) -> bool:
        return False

    def clay_get_invalid_webhook_error_msg(self) -> str:
        return "Clay export is not supported for Fathom messages"

    def clay_get_row(self) -> dict[str, Any]:
        return {}

    def attio_is_valid_webhook(self) -> bool:
        # Fathom "messages" are action-items / one-line follow-ups that don't
        # cleanly map to Attio yet. Returning False keeps the contract uniform
        # so the dispatcher can be pointed at this webhook without crashing,
        # but no Attio writes happen.
        return False

    def attio_get_invalid_webhook_error_msg(self) -> str:
        return (
            "Fathom messages are not currently exported to Attio "
            "(UpsertNote mapping deferred)"
        )

    def attio_get_operations(self) -> list[Any]:
        return []
