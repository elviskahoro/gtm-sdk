from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from libs.attio.contracts import ReliabilityEnvelope
from libs.attio.models import NoteInput


def test_find_note_by_title_returns_none_when_no_match() -> None:
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = []

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import find_note_by_title

        out = find_note_by_title(
            parent_object="companies",
            parent_record_id="cid",
            title="missing",
        )
    assert out is None


def test_find_note_by_title_returns_existing_note_id() -> None:
    note_match = MagicMock()
    note_match.title = "Snowflake CSV annotation — 2026-05-25"
    note_match.id.note_id = "note-1"
    note_other = MagicMock()
    note_other.title = "Other"
    note_other.id.note_id = "note-2"
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = [note_other, note_match]

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import find_note_by_title

        out = find_note_by_title(
            parent_object="companies",
            parent_record_id="cid",
            title="Snowflake CSV annotation — 2026-05-25",
        )
    assert out == "note-1"


def test_create_note_skips_post_when_title_matches() -> None:
    note_match = MagicMock()
    note_match.title = "Snowflake CSV annotation — 2026-05-25"
    note_match.id.note_id = "existing-note"
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = [note_match]

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import create_note

        envelope: ReliabilityEnvelope = create_note(
            input=NoteInput(
                parent_object="companies",
                parent_record_id="cid",
                title="Snowflake CSV annotation — 2026-05-25",
                content="duplicate body",
                format="markdown",
            ),
            apply=True,
        )
    assert envelope.action == "noop"
    assert envelope.record_id == "existing-note"
    fake_client.notes.post_v2_notes.assert_not_called()


def test_create_note_preview_does_not_post() -> None:
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = []

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import create_note

        envelope = create_note(
            input=NoteInput(
                parent_object="companies",
                parent_record_id="cid",
                title="t",
                content="body",
                format="markdown",
                created_at=datetime(2025, 1, 5, tzinfo=UTC),
            ),
            apply=False,
        )
    assert envelope.action == "noop"
    assert envelope.record_id is None
    fake_client.notes.post_v2_notes.assert_not_called()


def test_create_note_passes_created_at_through_to_sdk_boundary() -> None:
    """The Notes API accepts created_at for backdating; verify it's wired."""
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = []
    created = MagicMock()
    created.id.note_id = "new-note"
    fake_client.notes.post_v2_notes.return_value.data = created
    captured: dict[str, object] = {}

    def _spy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return fake_client.notes.post_v2_notes.return_value

    fake_client.notes.post_v2_notes.side_effect = _spy

    backdated = datetime(2025, 1, 5, tzinfo=UTC)
    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import create_note

        envelope = create_note(
            input=NoteInput(
                parent_object="companies",
                parent_record_id="cid",
                title="t",
                content="body",
                format="markdown",
                created_at=backdated,
            ),
            apply=True,
        )
    assert envelope.action == "created"
    request_obj = captured["data"]
    assert getattr(request_obj, "created_at", None) is not None
    assert "2025-01-05" in str(getattr(request_obj, "created_at"))
