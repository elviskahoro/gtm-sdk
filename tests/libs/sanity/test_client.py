"""Tests for the Sanity Query API client error handling."""

from unittest.mock import MagicMock, patch

import pytest

from libs.sanity.client import SanityConfig, query
from libs.sanity.errors import SanityQueryError


def _response(*, ok=True, status=200, json_value=None, json_raises=False, text=""):
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status
    resp.text = text
    if json_raises:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_value
    return resp


def test_query_raises_on_http_error():
    with patch(
        "libs.sanity.client.requests.get",
        return_value=_response(ok=False, status=500, text="boom"),
    ):
        with pytest.raises(SanityQueryError):
            query("*", config=SanityConfig())


def test_query_raises_on_non_json_body():
    with patch(
        "libs.sanity.client.requests.get",
        return_value=_response(json_raises=True, text="<html>"),
    ):
        with pytest.raises(SanityQueryError):
            query("*", config=SanityConfig())


def test_query_raises_when_payload_not_dict():
    with patch(
        "libs.sanity.client.requests.get",
        return_value=_response(json_value=["unexpected"]),
    ):
        with pytest.raises(SanityQueryError):
            query("*", config=SanityConfig())


def test_query_returns_result():
    with patch(
        "libs.sanity.client.requests.get",
        return_value=_response(json_value={"result": [1, 2]}),
    ):
        assert query("*", config=SanityConfig()) == [1, 2]


def test_no_env_token_ignores_ambient_token(monkeypatch):
    monkeypatch.setenv("SANITY_API_TOKEN", "ambient-secret")
    captured: dict[str, dict[str, str]] = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["headers"] = headers or {}
        return _response(json_value={"result": []})

    with patch("libs.sanity.client.requests.get", side_effect=fake_get):
        query("*", config=SanityConfig(), allow_env_token=False)
    assert "Authorization" not in captured["headers"]

    with patch("libs.sanity.client.requests.get", side_effect=fake_get):
        query("*", config=SanityConfig(), allow_env_token=True)
    assert captured["headers"]["Authorization"] == "Bearer ambient-secret"
