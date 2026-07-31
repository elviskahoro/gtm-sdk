"""Minimal, synchronous client for Clay webhook-table ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

CLAY_WEBHOOK_AUTH_HEADER = "x-clay-webhook-auth"
DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ClayDeliveryResult:
    """The successful response metadata retained for webhook observability."""

    status_code: int


def post_row(
    *,
    webhook_url: str,
    auth_token: str,
    row: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ClayDeliveryResult:
    """POST one JSON row to a Clay webhook table or raise on delivery failure."""
    response = requests.post(
        webhook_url,
        json=row,
        headers={CLAY_WEBHOOK_AUTH_HEADER: auth_token},
        timeout=timeout,
    )
    response.raise_for_status()
    return ClayDeliveryResult(status_code=response.status_code)
