from __future__ import annotations

from src.octolens.webhook import Webhook


def test_required_api_keys() -> None:
    assert Webhook.required_api_keys() == ["ATTIO_API_KEY"]
