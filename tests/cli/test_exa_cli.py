"""Tests for cli/exa subapp — verify wiring to Modal `.remote()`."""

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from cli.main import app


class _FakeModalFunction:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def remote(self, **kwargs):
        self.calls.append(kwargs)
        # Mimic SearchResponse.model_dump shape
        return {
            "request_id": "req_x",
            "search_type": "auto",
            "results": [],
            "output": None,
            "cost_dollars": 0.001,
            "_received_query": kwargs.get("payload", {}).get("query"),
        }


class _FakeModalRegistry:
    def __init__(self) -> None:
        self.functions: dict[str, _FakeModalFunction] = {}

    def from_name(self, _app_name: str, function_name: str):
        fn = self.functions.get(function_name)
        if fn is None:
            fn = _FakeModalFunction(function_name)
            self.functions[function_name] = fn
        return fn


def _patch_modal(monkeypatch, *modules) -> _FakeModalRegistry:
    registry = _FakeModalRegistry()
    for module in modules:
        monkeypatch.setattr(module.modal.Function, "from_name", registry.from_name)
    return registry


def test_exa_subapp_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["exa", "--help"])
    assert result.exit_code == 0
    assert "search" in result.stdout
    assert "find-companies" in result.stdout
    assert "find-people" in result.stdout


def test_search_routes_payload_to_modal(monkeypatch) -> None:
    import cli.exa.search as exa_search

    registry = _patch_modal(monkeypatch, exa_search)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["exa", "search", "snowflake", "--num-results", "5"],
    )

    assert result.exit_code == 0, result.stdout
    fn = registry.functions["exa_search"]
    assert len(fn.calls) == 1
    payload = fn.calls[0]["payload"]
    assert payload["query"] == "snowflake"
    assert payload["num_results"] == 5


def test_find_companies_routes_payload(monkeypatch) -> None:
    import cli.exa.companies as exa_companies

    registry = _patch_modal(monkeypatch, exa_companies)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["exa", "find-companies", "datadog", "--num-results", "4"],
    )

    assert result.exit_code == 0, result.stdout
    fn = registry.functions["exa_find_companies"]
    payload = fn.calls[0]["payload"]
    assert payload["query"] == "datadog"
    assert payload["num_results"] == 4


def test_find_people_routes_payload(monkeypatch) -> None:
    import cli.exa.people as exa_people

    registry = _patch_modal(monkeypatch, exa_people)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["exa", "find-people", "olaf carlson-wee", "--num-results", "2"],
    )

    assert result.exit_code == 0, result.stdout
    fn = registry.functions["exa_find_people"]
    payload = fn.calls[0]["payload"]
    assert payload["query"] == "olaf carlson-wee"
    assert payload["num_results"] == 2


def test_search_with_json_override(monkeypatch) -> None:
    import cli.exa.search as exa_search

    registry = _patch_modal(monkeypatch, exa_search)
    runner = CliRunner()
    payload_json = json.dumps(
        {"query": "from json", "type": "fast", "num_results": 7},
    )
    result = runner.invoke(app, ["exa", "search", "--json", payload_json])

    assert result.exit_code == 0, result.stdout
    payload = registry.functions["exa_search"].calls[0]["payload"]
    assert payload["query"] == "from json"
    assert payload["type"] == "fast"
    assert payload["num_results"] == 7


def test_search_all_blank_domains_omitted_from_payload(monkeypatch) -> None:
    """Regression (roborev): ``--include-domains ","`` collapses to nothing
    after stripping. Omit the field entirely so it doesn't reach the model
    as ``[]`` (which the new validator rejects as "must be non-empty")."""
    import cli.exa.search as exa_search

    registry = _patch_modal(monkeypatch, exa_search)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["exa", "search", "x", "--include-domains", ",  ,"],
    )

    assert result.exit_code == 0, result.stdout
    payload = registry.functions["exa_search"].calls[0]["payload"]
    assert "include_domains" not in payload


def test_search_strips_empty_domain_segments(monkeypatch) -> None:
    """Regression (roborev): ``--include-domains "a,,b,"`` must drop empty
    segments instead of sending empty strings to Exa."""
    import cli.exa.search as exa_search

    registry = _patch_modal(monkeypatch, exa_search)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "exa",
            "search",
            "x",
            "--include-domains",
            "a.com,,b.com,",
            "--exclude-domains",
            ",c.com,",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = registry.functions["exa_search"].calls[0]["payload"]
    assert payload["include_domains"] == ["a.com", "b.com"]
    assert payload["exclude_domains"] == ["c.com"]


def test_search_with_output_schema_json(monkeypatch) -> None:
    import cli.exa.search as exa_search

    registry = _patch_modal(monkeypatch, exa_search)
    runner = CliRunner()
    schema = '{"type":"object","required":["domain"],"properties":{"domain":{"type":"string"}}}'
    result = runner.invoke(
        app,
        [
            "exa",
            "search",
            "acme",
            "--output-schema-json",
            schema,
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = registry.functions["exa_search"].calls[0]["payload"]
    assert payload["output_schema"]["required"] == ["domain"]
