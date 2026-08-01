from __future__ import annotations

import json
from pathlib import Path

from src.fathom.webhook.message import Webhook

FIXTURE = Path("api/samples/fathom.message.redacted.json")


def _load() -> Webhook:
    return Webhook.model_validate(json.loads(FIXTURE.read_text()))


def test_live_message_fixture_parses_and_requires_transcript() -> None:
    webhook = _load()

    assert webhook.etl_is_valid_webhook() is True
    transcript = webhook.transcript
    assert transcript is not None
    assert len(transcript) == 2
    assert transcript[1].speaker.matched_calendar_invitee_email is None


def test_etl_json_is_one_flattened_row_per_transcript_message() -> None:
    rows = [json.loads(line) for line in _load().etl_get_json().splitlines()]

    assert len(rows) == 2
    assert rows[0]["recording_id"] == 168379684
    assert rows[0]["speaker_display_name"] == "Host"
    assert rows[0]["speaker_matched_calendar_invitee_email"] == "host@dlthub.com"
    assert rows[0]["text"] == "Welcome to the demo."
    assert rows[0]["timestamp"] == "00:00:00"
    assert rows[0]["id"] == "168379684-00000"
    assert rows[1]["speaker_matched_calendar_invitee_email"] is None
    assert "transcript" not in rows[0]


def test_empty_or_null_transcript_is_not_valid_for_message_etl() -> None:
    empty = _load().model_copy(update={"transcript": []})
    null = _load().model_copy(update={"transcript": None})

    assert empty.etl_is_valid_webhook() is False
    assert null.etl_is_valid_webhook() is False
    assert "no transcript" in empty.etl_get_invalid_webhook_error_msg()


def test_etl_filename_uses_recording_timestamp_id_and_title() -> None:
    assert _load().etl_get_file_name() == "20260729160112-168379684-host_guest.jsonl"
