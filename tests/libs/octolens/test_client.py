"""Tests for libs/octolens/client.py — v2 API pagination, retry, rate-limit.

httpx.MockTransport stands in for the network so these run offline; the client
accepts a `transport=` only for this purpose.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from libs.octolens.client import OctolensApiError, OctolensClient


def _client(handler: Any) -> OctolensClient:
    return OctolensClient("test-key", transport=httpx.MockTransport(handler))


def test_list_mentions_paginates_until_cursor_exhausted() -> None:
    pages: dict[Any, dict[str, Any]] = {
        None: {
            "data": [
                {"sourceId": "a", "url": "u1"},
                {"sourceId": "b", "url": "u2"},
            ],
            "pagination": {"nextCursor": "c1"},
        },
        "c1": {
            "data": [{"sourceId": "c", "url": "u3"}],
            "pagination": {"nextCursor": None},
        },
    }
    seen_cursors: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cursor = body.get("cursor")
        seen_cursors.append(cursor)
        return httpx.Response(200, json=pages[cursor])

    mentions = list(_client(handler).list_mentions())
    assert [m.source_id for m in mentions] == ["a", "b", "c"]
    # Second request carries the cursor from the first page's pagination.
    assert seen_cursors == [None, "c1"]


def test_list_mentions_respects_page_size_and_include_all() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"data": [], "pagination": {"nextCursor": None}},
        )

    list(_client(handler).list_mentions(include_all=True, page_size=50))
    assert captured["limit"] == 50
    assert captured["includeAll"] is True


def test_list_mentions_rejects_out_of_range_page_size() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(
            200,
            json={"data": [], "pagination": {"nextCursor": None}},
        )

    with pytest.raises(ValueError, match="page_size"):
        list(_client(handler).list_mentions(page_size=101))


def test_list_mentions_retries_transient_429(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("libs.octolens.client.time.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={
                    "error": {"code": "RATE_LIMITED", "message": "slow", "status": 429},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [{"sourceId": "a", "url": "u"}],
                "pagination": {"nextCursor": None},
            },
        )

    mentions = list(_client(handler).list_mentions())
    assert [m.source_id for m in mentions] == ["a"]
    assert calls["n"] == 2  # one retry after the 429


def test_retry_after_is_clamped_to_max(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    def _record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("libs.octolens.client.time.sleep", _record_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # Absurd Retry-After — must be clamped, not honored literally.
            return httpx.Response(429, headers={"Retry-After": "999999"}, json={})
        return httpx.Response(
            200,
            json={"data": [], "pagination": {"nextCursor": None}},
        )

    list(_client(handler).list_mentions())
    # Clamped to _MAX_RATELIMIT_SLEEP_SECONDS (3600s), not the 999999s header.
    assert slept == [3600.0]


def test_list_mentions_retries_transient_408(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("libs.octolens.client.time.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(408, json={})  # Request Timeout — transient
        return httpx.Response(
            200,
            json={
                "data": [{"sourceId": "a", "url": "u"}],
                "pagination": {"nextCursor": None},
            },
        )

    mentions = list(_client(handler).list_mentions())
    assert [m.source_id for m in mentions] == ["a"]
    assert calls["n"] == 2  # 408 retried, not raised


def test_list_mentions_raises_on_terminal_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {"code": "UNAUTHORIZED", "message": "bad key", "status": 401},
            },
        )

    with pytest.raises(OctolensApiError) as exc:
        list(_client(handler).list_mentions())
    assert "UNAUTHORIZED" in str(exc.value)


def test_list_mentions_sleeps_when_rate_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    def _record_sleep(seconds: float) -> None:
        slept.append(seconds)

    def _fixed_time() -> float:
        return 1000.0

    monkeypatch.setattr("libs.octolens.client.time.sleep", _record_sleep)
    monkeypatch.setattr("libs.octolens.client.time.time", _fixed_time)

    pages: dict[Any, dict[str, Any]] = {
        None: {
            "data": [{"sourceId": "a", "url": "u1"}],
            "pagination": {"nextCursor": "c1"},
        },
        "c1": {
            "data": [{"sourceId": "b", "url": "u2"}],
            "pagination": {"nextCursor": None},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = json.loads(request.content).get("cursor")
        headers = (
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1005"}
            if cursor is None
            else {}
        )
        return httpx.Response(200, headers=headers, json=pages[cursor])

    list(_client(handler).list_mentions())
    # Exhausted budget on page 1 → proactively sleep until reset (1005 - 1000).
    assert slept == [pytest.approx(5.0, abs=0.01)]


def test_list_mentions_skips_unparseable_item_without_aborting() -> None:
    # A type-mismatched modeled field (relevance_score) on one item must not
    # abort the pull — it is logged and skipped, the rest still yield.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"sourceId": "ok1", "url": "u1"},
                    {"sourceId": "bad", "url": "u2", "relevanceScore": "not-a-number"},
                    {"sourceId": "ok2", "url": "u3"},
                ],
                "pagination": {"nextCursor": None},
            },
        )

    mentions = list(_client(handler).list_mentions())
    assert [m.source_id for m in mentions] == ["ok1", "ok2"]


def test_list_mentions_tolerates_null_nested_keyword() -> None:
    # A null keyword text inside keywords[] must not discard the whole mention.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "sourceId": "a",
                        "url": "u",
                        "keywords": [{"id": 1, "keyword": None}],
                    },
                ],
                "pagination": {"nextCursor": None},
            },
        )

    mentions = list(_client(handler).list_mentions())
    assert [m.source_id for m in mentions] == ["a"]
    assert mentions[0].keywords[0].keyword is None


def test_list_keywords_returns_data_array() -> None:
    keywords = [
        {"id": 30412, "keyword": "dlthub", "tag": "own_brand"},
        {"id": 31833, "keyword": "dlt", "tag": "own_brand"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/keywords"
        return httpx.Response(200, json={"data": keywords})

    result = _client(handler).list_keywords()
    assert {kw["keyword"]: kw["id"] for kw in result} == {"dlthub": 30412, "dlt": 31833}


def test_from_env_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCTOLENS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OCTOLENS_API_KEY"):
        OctolensClient.from_env()
