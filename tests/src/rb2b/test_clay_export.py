# ruff: noqa: I001, S101
from __future__ import annotations

from pathlib import Path

import orjson

from src.rb2b.webhook.visit import Webhook

SAMPLES_DIR = Path(__file__).resolve().parents[3] / "api" / "samples"


def test_clay_row_includes_identified_person_and_company() -> None:
    payload = orjson.loads(
        (SAMPLES_DIR / "rb2b.visit.person_and_company.redacted.json").read_bytes(),
    )
    webhook = Webhook.model_validate(payload)

    assert webhook.clay_is_valid_webhook()
    row = webhook.clay_get_row()
    assert row["event_id"] == "evt_person_company_001"
    assert row["event_type"] == "first_visit"
    assert row["business_email"] == "alice@example.test"
    assert row["company_domain"] == "example.test"


def test_clay_rejects_company_only_and_anonymous_visits() -> None:
    for name in (
        "rb2b.visit.company_only.redacted.json",
        "rb2b.visit.anonymous.redacted.json",
    ):
        payload = orjson.loads((SAMPLES_DIR / name).read_bytes())
        assert not Webhook.model_validate(payload).clay_is_valid_webhook()
