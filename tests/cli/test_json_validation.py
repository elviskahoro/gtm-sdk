from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from cli.json_validation import emit_json_payload_validation_telemetry


class _Sample(BaseModel):
    name: str = Field(min_length=1)


def test_emit_json_payload_validation_telemetry_validation_error(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _capture(name: str, attrs: dict[str, Any]) -> None:
        calls.append((name, attrs))

    monkeypatch.setattr(
        "cli.json_validation.emit_cli_event",
        _capture,
    )

    raw = '{"name": ""}'
    try:
        _Sample.model_validate_json(raw)
    except ValidationError as exc:
        emit_json_payload_validation_telemetry("test.cmd", exc, _Sample, raw)

    assert len(calls) == 1
    name, attrs = calls[0]
    assert name == "cli.json_validation_error"
    assert attrs["command"] == "test.cmd"
    assert attrs["raw_fields"] == ["name"]
    assert "name" in attrs["valid_fields"]
    assert attrs["errors"]


def test_emit_json_payload_validation_telemetry_json_decode(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _capture(name: str, attrs: dict[str, Any]) -> None:
        calls.append((name, attrs))

    monkeypatch.setattr(
        "cli.json_validation.emit_cli_event",
        _capture,
    )

    raw = "{not json"
    exc = json.JSONDecodeError("Expecting property name", raw, 1)
    emit_json_payload_validation_telemetry("test.cmd", exc, _Sample, raw)

    assert len(calls) == 1
    _, attrs = calls[0]
    assert attrs["errors"][0]["type"] == "json_invalid"
