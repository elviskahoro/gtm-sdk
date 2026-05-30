from __future__ import annotations

import json
from pathlib import Path

from fathom_python import models as M

from libs.fathom import Webhook, webhook_from_sdk_meeting

SAMPLE = (
    Path(__file__).resolve().parents[3]
    / "api"
    / "samples"
    / "fathom.list_meetings.redacted.json"
)


def _meetings_from_sample() -> list[M.Meeting]:
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    return [M.Meeting.model_validate(item) for item in raw]


def test_maps_sample_meetings_to_valid_webhooks() -> None:
    meetings = _meetings_from_sample()
    webhooks = [webhook_from_sdk_meeting(m) for m in meetings]
    assert all(isinstance(w, Webhook) for w in webhooks)
    assert [w.recording_id for w in webhooks] == [111111, 222222]


def test_null_recorded_by_team_coerced_to_empty_string() -> None:
    # First sample meeting has recorded_by.team == null; the webhook model
    # requires a str.
    meeting = _meetings_from_sample()[0]
    webhook = webhook_from_sdk_meeting(meeting)
    assert webhook.recorded_by.team == ""


def test_invitee_with_null_email_is_dropped() -> None:
    # First sample meeting has one valid invitee and one with a null email.
    meeting = _meetings_from_sample()[0]
    webhook = webhook_from_sdk_meeting(meeting)
    assert len(webhook.calendar_invitees) == 1
    assert webhook.calendar_invitees[0].email == "host@dlthub.com"


def test_optional_summary_and_action_items_tolerated() -> None:
    # Second sample meeting has null default_summary / action_items / invitees.
    meeting = _meetings_from_sample()[1]
    webhook = webhook_from_sdk_meeting(meeting)
    assert webhook.default_summary is None
    assert webhook.action_items is None
    assert webhook.calendar_invitees == []


def _meeting_dict(**overrides: object) -> dict[str, object]:
    """A complete valid Meeting payload as a dict.

    Built and validated via ``Meeting.model_validate`` (not the typed
    constructor) so the SDK's Pydantic coercion handles ISO strings / enum
    values — passing those positionally to ``Meeting(...)`` trips pyright's
    reportArgumentType since the fields are typed ``datetime`` / enum.
    """
    base: dict[str, object] = {
        "title": "t",
        "meeting_title": None,
        "recording_id": 1,
        "url": "https://fathom.video/calls/1",
        "share_url": "https://fathom.video/share/1",
        "created_at": "2026-05-12T14:00:00Z",
        "scheduled_start_time": "2026-05-12T14:00:00Z",
        "scheduled_end_time": "2026-05-12T15:00:00Z",
        "recording_start_time": "2026-05-12T14:00:00Z",
        "recording_end_time": "2026-05-12T15:00:00Z",
        "calendar_invitees_domains_type": "only_internal",
        "transcript_language": "en",
        "calendar_invitees": [],
        "recorded_by": {
            "name": "H",
            "email": "h@dlthub.com",
            "email_domain": "dlthub.com",
            "team": None,
        },
    }
    base.update(overrides)
    return base


def test_transcript_and_crm_matches_preserved() -> None:
    # The mapper must be lossless: transcript and crm_matches share the SDK and
    # webhook shapes, so they round-trip rather than being dropped.
    meeting = M.Meeting.model_validate(
        _meeting_dict(
            recording_id=7,
            transcript=[
                {
                    "speaker": {
                        "display_name": "H",
                        "matched_calendar_invitee_email": None,
                    },
                    "text": "hi",
                    "timestamp": "00:01",
                },
            ],
            crm_matches={
                "companies": [{"name": "Acme", "record_url": "https://x"}],
                "contacts": [],
                "deals": [],
                "error": None,
            },
        ),
    )
    webhook = webhook_from_sdk_meeting(meeting)
    assert webhook.transcript is not None
    assert webhook.transcript[0].text == "hi"
    assert webhook.crm_matches is not None
    assert webhook.crm_matches.companies[0].name == "Acme"


def test_null_summary_fields_coerced() -> None:
    meeting = M.Meeting.model_validate(
        _meeting_dict(
            default_summary={"template_name": None, "markdown_formatted": None},
            action_items=None,
        ),
    )
    webhook = webhook_from_sdk_meeting(meeting)
    assert webhook.default_summary is not None
    assert webhook.default_summary.template_name == ""
    assert webhook.default_summary.markdown_formatted == ""
