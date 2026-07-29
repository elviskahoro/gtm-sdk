# ruff: noqa: S101
from __future__ import annotations

from typing import Any

import pytest

from src.clay.export import ClayRowEventIdError, execute


@pytest.mark.parametrize("row", [{}, {"event_id": 1}])
def test_execute_rejects_missing_or_non_string_event_id(row: dict[str, Any]) -> None:
    with pytest.raises(ClayRowEventIdError):
        execute(
            webhook_url="https://clay.test/hook",
            auth_token="test-token",  # noqa: S106 # nosec B106
            row=row,
        )
