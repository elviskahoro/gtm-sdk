# ruff: noqa: S101
from __future__ import annotations

import pytest
import requests

from libs.clay_http.client import CLAY_WEBHOOK_AUTH_HEADER, post_row

HTTP_ACCEPTED = 202


def test_post_row_sends_json_and_clay_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = HTTP_ACCEPTED

        @staticmethod
        def raise_for_status() -> None:
            return None

    def fake_post(url: str, **kwargs: object) -> Response:
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("libs.clay_http.client.requests.post", fake_post)

    result = post_row(
        webhook_url="https://clay.test/hook",
        auth_token="test-token",  # noqa: S106 # nosec B106
        row={"event_id": "evt-1"},
    )

    assert result.status_code == HTTP_ACCEPTED
    assert captured == {
        "url": "https://clay.test/hook",
        "json": {"event_id": "evt-1"},
        "headers": {CLAY_WEBHOOK_AUTH_HEADER: "test-token"},
        "timeout": 10.0,
    }


def test_post_row_propagates_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    response = requests.Response()
    response.status_code = 500
    response.url = "https://clay.test/hook"

    def fake_post(_url: str, **_kwargs: object) -> requests.Response:
        return response

    monkeypatch.setattr("libs.clay_http.client.requests.post", fake_post)

    with pytest.raises(requests.HTTPError):
        post_row(
            webhook_url="https://clay.test/hook",
            auth_token="test-token",  # noqa: S106 # nosec B106
            row={"event_id": "evt-1"},
        )
