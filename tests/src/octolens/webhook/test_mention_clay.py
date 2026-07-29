# ruff: noqa: I001, INP001, S101
from __future__ import annotations

from pathlib import Path

import orjson

from src.octolens.webhook.mention import Webhook

SAMPLES_DIR = Path(__file__).resolve().parents[4] / "api" / "samples"


def test_clay_row_includes_curated_mention_fields() -> None:
    payload = orjson.loads(
        (SAMPLES_DIR / "octolens.mention.created.twitter.redacted.json").read_bytes(),
    )
    webhook = Webhook.model_validate(payload)

    assert webhook.clay_is_valid_webhook()
    row = webhook.clay_get_row()
    assert row["event_id"] == "octolens:twitter:0000000000000000000"
    assert row["mention_url"] == payload["data"]["url"]
    assert row["keywords"] == ["example_keyword"]
    assert row["tags"] == ["competitor_mention", "industry_insights"]


def test_clay_rejects_low_relevance_and_updates() -> None:
    payload = orjson.loads(
        (SAMPLES_DIR / "octolens.mention.created.twitter.redacted.json").read_bytes(),
    )
    payload["data"]["relevanceScore"] = "low"
    assert not Webhook.model_validate(payload).clay_is_valid_webhook()

    payload["data"]["relevanceScore"] = "high"
    payload["action"] = "mention_updated"
    assert not Webhook.model_validate(payload).clay_is_valid_webhook()
