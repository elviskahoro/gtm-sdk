"""Source-agnostic Clay webhook-table row dispatcher."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from libs.clay_http import post_row


class ClayRowEventIdError(TypeError):
    """Raised when a source row violates Clay's stable-ID contract."""


@dataclass(frozen=True)
class ExecuteResult:
    event_id: str
    status_code: int

    def body(self) -> str:
        return json.dumps(
            {
                "event_id": self.event_id,
                "status_code": self.status_code,
                "success": True,
            },
            sort_keys=True,
        )


def execute(
    *,
    webhook_url: str,
    auth_token: str,
    row: dict[str, Any],
) -> ExecuteResult:
    """Deliver a validated source row to Clay and return a small success body."""
    event_id = row.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        msg = "Clay rows require a non-empty canonical event_id for deduplication"
        raise ClayRowEventIdError(msg)
    result = post_row(
        webhook_url=webhook_url,
        auth_token=auth_token,
        row=row,
    )
    return ExecuteResult(event_id=event_id, status_code=result.status_code)
