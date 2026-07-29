from __future__ import annotations

from typing import Any

import pytest
import requests

from libs.clay_http.client import CLAY_WEBHOOK_AUTH_HEADER, post_row


def test_post_row_sends_json_and_clay_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Response:
        status_code = 202

        @staticmethod
        def raise_for_status() -> None:
            return None

    def fake_post(url: str, **kwargs: Any) -> Response:
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("libs.clay_http.client.requests.post", fake_post)

    result = post_row(
        webhook_url="https://clay.test/hook",
        auth_token="secret",
        row={"event_id": "evt-1"},
    )

    assert result.status_code == 202
    assert captured == {
        "url": "https://clay.test/hook",
        "json": {"event_id": "evt-1"},
        "headers": {CLAY_WEBHOOK_AUTH_HEADER: "secret"},
        "timeout": 10.0,
    }


def test_post_row_propagates_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    response = requests.Response()
    response.status_code = 500
    response.url = "https://clay.test/hook"

    def fake_post(_url: str, **_kwargs: Any) -> requests.Response:
        return response

    monkeypatch.setattr("libs.clay_http.client.requests.post", fake_post)

    with pytest.raises(requests.HTTPError):
        post_row(
            webhook_url="https://clay.test/hook",
            auth_token="secret",
            row={"event_id": "evt-1"},
        )
