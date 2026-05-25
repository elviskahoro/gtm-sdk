# trunk-ignore-all(pyright/reportPrivateUsage,pyright/reportUnusedFunction): test fixtures legitimately reach into the structured logger's contextvars to isolate state
from __future__ import annotations

import re
from typing import Any

import orjson
import pytest
from fastapi import Request

from libs.logging import structured
from libs.logging.structured import (
    extract_or_generate_request_id,
    get_request_id,
    get_source,
    log,
    set_request_id,
    set_source,
    webhook_request_context,
)


def _read_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out.strip()
    if not out:
        return []
    return [orjson.loads(line) for line in out.splitlines()]


def _make_request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "headers": raw_headers,
        "path": "/",
        "query_string": b"",
    }
    return Request(scope)  # pyright: ignore[reportArgumentType]


@pytest.fixture(autouse=True)
def _isolated_contextvars() -> Any:
    # Pytest runs tests in a single asyncio-free thread, so contextvars set in
    # one test leak into the next. Snapshot the two we manage and restore.
    prev_request_id = structured._REQUEST_ID.get()
    prev_source = structured._SOURCE.get()
    try:
        yield
    finally:
        structured._REQUEST_ID.set(prev_request_id)
        structured._SOURCE.set(prev_source)


def test_log_emits_json_with_standard_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log("test.event", foo="bar", count=3)
    [payload] = _read_lines(capsys)
    assert payload["event"] == "test.event"
    assert payload["foo"] == "bar"
    assert payload["count"] == 3
    assert "ts" in payload
    assert "source" in payload
    assert "request_id" in payload


def test_log_uses_contextvars_for_source_and_request_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_source("rb2b")
    set_request_id("abc-123")
    log("test.event")
    [payload] = _read_lines(capsys)
    assert payload["source"] == "rb2b"
    assert payload["request_id"] == "abc-123"


def test_log_does_not_raise_on_unserializable_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Unserialisable:
        pass

    log("test.event", obj=Unserialisable())
    [payload] = _read_lines(capsys)
    # Fallback path drops the bad field but keeps the event recognisable.
    assert payload["event"] == "test.event"
    assert "log_error" in payload


def test_extract_or_generate_request_id_uses_header_when_present() -> None:
    request = _make_request({"X-Request-Id": "known-id"})
    assert extract_or_generate_request_id(request) == "known-id"


def test_extract_or_generate_request_id_falls_back_to_uuid7() -> None:
    request = _make_request()
    generated = extract_or_generate_request_id(request)
    # uuid7 string format: 8-4-4-4-12 hex. version nibble == 7.
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}",
        generated,
    )


def test_webhook_request_context_resets_on_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = _make_request({"X-Request-Id": "ctx-id"})
    assert get_request_id() is None
    with webhook_request_context(request) as rid:
        assert rid == "ctx-id"
        assert get_request_id() == "ctx-id"
        log("test.event")
    assert get_request_id() is None
    [payload] = _read_lines(capsys)
    assert payload["request_id"] == "ctx-id"


def test_set_source_persists_in_current_context() -> None:
    assert get_source() is None
    set_source("attio")
    assert get_source() == "attio"


def test_log_includes_iso8601_timestamp_with_timezone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log("test.event")
    [payload] = _read_lines(capsys)
    assert payload["ts"].endswith("+00:00")


def test_module_export_surface_matches_documented_api() -> None:
    # Guard against accidental rename of the public functions documented in
    # design/backlog-202605171803-webhook_structured_logs-plan-01.md.
    for name in (
        "log",
        "set_source",
        "set_request_id",
        "get_request_id",
        "extract_or_generate_request_id",
        "webhook_request_context",
    ):
        assert hasattr(structured, name), name
