"""Source-agnostic Clay webhook-table row dispatcher."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from libs.clay_http import post_row


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
    result = post_row(
        webhook_url=webhook_url,
        auth_token=auth_token,
        row=row,
    )
    event_id = row["event_id"]
    assert isinstance(event_id, str)
    return ExecuteResult(event_id=event_id, status_code=result.status_code)
