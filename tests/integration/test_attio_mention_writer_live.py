"""Live regression test for AI-290: assert-may-create preserves source identity.

The AI-290 bug lived in `libs/attio/values.build_update_mention_values`, which
stripped `source_platform` / `source_id` on the update path. Attio's assert
endpoint creates the record on the first delivery seen for a `mention_url`, so
if that first delivery is a `mention_updated` (e.g. the create was dropped or
replayed out of order), the new record landed without its source identity.
Commit 5db80c8 collapsed the create/update value builders into one
`build_mention_values` that always sends both fields.

This test hits live Attio with a `mention_updated` for a guaranteed-fresh URL,
then queries the resulting record to confirm `source_platform`, `source_id`,
and `last_action` are all populated correctly. No Modal/webhook layer is
exercised — the bug was at the value-builder layer below them, so a direct
`upsert_mention` call is the narrowest test that catches the regression.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import pytest

from libs.attio.mentions import upsert_mention
from libs.attio.models import MentionInput

pytestmark = pytest.mark.integration


def test_upsert_mention_assert_may_create_preserves_source_identity(
    attio_api_key: str,  # noqa: ARG001 — fixture handles credential skip
    social_mention_bootstrapped: None,  # noqa: ARG001 — fixture skips if obj missing
    client: Any,
    created_mention_record_ids: list[str],
    cleanup_mention_records: None,  # noqa: ARG001 — autouse-style teardown
) -> None:
    # Use token_hex (not time.time()) so same-second reruns don't collide,
    # and embed a sentinel in the URL so a half-cleaned-up record from a
    # prior failed run is easy to spot and delete by hand.
    nonce = secrets.token_hex(8)
    mention_url = f"https://example.com/ai290-live-{nonce}"
    source_id = f"ai290-live-{nonce}"
    # "reddit" matches the AI-290 unit fixture (tests/libs/attio/test_mentions.py)
    # and is a real Octolens-emitted platform — i.e. it's already a select option
    # in social_mention.source_platform, so this test doesn't pollute the
    # open-vocab dropdown. Avoid "linkedin" / "github" here: those trigger
    # UpsertPerson side-effects in the higher-level dispatcher, which is out of
    # scope for the value-builder layer this test exercises.
    source_platform = "reddit"

    envelope = upsert_mention(
        MentionInput(
            mention_url=mention_url,
            last_action="mention_updated",
            source_platform=source_platform,
            source_id=source_id,
            mention_body="AI-290 live regression — assert-may-create branch.",
            mention_timestamp=datetime.now(tz=timezone.utc),
            author_handle="ai290-live-bot",
            primary_keyword="ai290-live-test",
        ),
    )
    assert envelope.success, envelope
    assert envelope.record_id, envelope
    created_mention_record_ids.append(envelope.record_id)

    # The envelope's `action` field reports the SDK's create-vs-update signal;
    # if Attio truly created a new record here (as expected for a fresh URL),
    # the assert-may-create branch was the path under test.
    assert envelope.action in ("created", "updated"), envelope

    # Read the record back through the query API (rather than trusting the
    # write payload) so we verify what Attio actually persisted.
    response = client.records.post_v2_objects_object_records_query(
        object="social_mention",
        filter_={"mention_url": mention_url},
        limit=1,
    )
    assert response.data, f"no social_mention record found for {mention_url}"
    record = response.data[0]
    values = (
        record.values.model_dump()
        if hasattr(record.values, "model_dump")
        else dict(record.values)
    )

    def _select_title(slug: str) -> str | None:
        raw = values.get(slug)
        if not raw:
            return None
        first = raw[0] if isinstance(raw, list) else raw
        if isinstance(first, dict):
            opt = first.get("option")
            if isinstance(opt, dict):
                return opt.get("title")
            return first.get("value")
        opt = getattr(first, "option", None)
        if opt is not None:
            return getattr(opt, "title", None)
        return getattr(first, "value", None)

    def _text(slug: str) -> str | None:
        raw = values.get(slug)
        if not raw:
            return None
        first = raw[0] if isinstance(raw, list) else raw
        if isinstance(first, dict):
            return first.get("value")
        return getattr(first, "value", None)

    assert _text("mention_url") == mention_url, values
    # AI-290: both must be populated even though the write went through the
    # `mention_updated` path. Before 5db80c8, these were stripped.
    assert _select_title("source_platform") == source_platform, values
    assert _text("source_id") == source_id, values
    assert _select_title("last_action") == "mention_updated", values
