from __future__ import annotations

import json
from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from libs.granola.api_client import GranolaApiClient
from libs.granola.errors import RateLimitError, SourceReadError


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_list_notes_paginates(monkeypatch) -> None:
    responses = [
        _FakeResponse({"notes": [{"id": "1"}], "next": "abc"}),
        _FakeResponse({"notes": [{"id": "2"}], "next": None}),
    ]

    def _urlopen(_req: Request, timeout: int = 30):
        return responses.pop(0)

    monkeypatch.setattr("libs.granola.api_client.urlopen", _urlopen)
    client = GranolaApiClient(api_key="k")
    notes = client.list_notes()
    assert [n["id"] for n in notes] == ["1", "2"]


def test_get_note_includes_transcript(monkeypatch) -> None:
    captured: list[str] = []

    def _urlopen(req: Request, timeout: int = 30):
        captured.append(req.full_url)
        return _FakeResponse({"id": "1"})

    monkeypatch.setattr("libs.granola.api_client.urlopen", _urlopen)
    client = GranolaApiClient(api_key="k")
    client.get_note("1", include_transcript=True)
    assert "include=transcript" in captured[0]


def test_429_maps_to_rate_limit(monkeypatch) -> None:
    def _urlopen(_req: Request, timeout: int = 30):
        raise HTTPError("u", 429, "rate", hdrs=Message(), fp=None)

    monkeypatch.setattr("libs.granola.api_client.urlopen", _urlopen)
    client = GranolaApiClient(api_key="k")
    with pytest.raises(RateLimitError):
        client.list_notes()


def test_malformed_json_maps_to_source_read(monkeypatch) -> None:
    class _Bad:
        def read(self) -> bytes:
            return b"{"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def _urlopen(_req: Request, timeout: int = 30):
        return _Bad()

    monkeypatch.setattr("libs.granola.api_client.urlopen", _urlopen)
    client = GranolaApiClient(api_key="k")
    with pytest.raises(SourceReadError):
        client.list_notes()
