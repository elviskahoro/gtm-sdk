# ruff: noqa: S101
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.clay.export import ClayRowEventIdError, execute


@pytest.mark.parametrize(
    "row",
    [{}, {"event_id": 1}, {"event_id": ""}, {"event_id": "  "}],
)
def test_execute_rejects_missing_or_non_string_event_id(row: dict[str, Any]) -> None:
    with pytest.raises(ClayRowEventIdError):
        execute(
            webhook_url="https://clay.test/hook",
            auth_token="test-token",  # noqa: S106 # nosec B106
            row=row,
        )


def test_execute_reposts_same_event_id_for_transport_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retries preserve the Clay dedupe key; Clay performs eventual cleanup."""
    event_ids: list[object] = []

    def fake_post_row(*, row: dict[str, Any], **_kwargs: object) -> SimpleNamespace:
        event_ids.append(row["event_id"])
        return SimpleNamespace(status_code=202)

    monkeypatch.setattr("src.clay.export.post_row", fake_post_row)
    row = {"event_id": "rb2b:evt-1", "email": "alice@example.test"}

    first = execute(
        webhook_url="https://clay.test/hook",
        auth_token="token",  # noqa: S106 # nosec B106
        row=row,
    )
    second = execute(
        webhook_url="https://clay.test/hook",
        auth_token="token",  # noqa: S106 # nosec B106
        row=row,
    )

    assert first.event_id == second.event_id == "rb2b:evt-1"
    assert event_ids == [
        "rb2b:evt-1",
        "rb2b:evt-1",
    ]
