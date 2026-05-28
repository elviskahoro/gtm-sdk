"""Tests for cli/attio/enrichment — backfill-domains command wiring."""

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
        # Mimic CompanyDomainBackfillReport.model_dump shape
        return {
            "patched": 0,
            "noop_had_domain": 0,
            "unresolved": 0,
            "skipped_race": 0,
            "failed": 0,
            "outcomes": [],
            "total_exa_cost_dollars": 0.0,
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


def test_enrichment_subapp_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["attio", "enrichment", "--help"])
    assert result.exit_code == 0
    assert "backfill-domains" in result.stdout


def test_backfill_domains_with_ext_tam_filter(monkeypatch) -> None:
    import cli.attio.enrichment as enrichment_mod

    registry = _FakeModalRegistry()
    monkeypatch.setattr(
        enrichment_mod.modal.Function,
        "from_name",
        registry.from_name,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "attio",
            "enrichment",
            "backfill-domains",
            "--ext-tam-filter",
            '{"source":"snowflake_scored_accounts_csv"}',
            "--limit",
            "10",
        ],
    )

    assert result.exit_code == 0, result.stdout
    fn = registry.functions["attio_backfill_company_domains"]
    assert len(fn.calls) == 1
    payload = fn.calls[0]["payload"]
    assert payload["ext_tam_filter"] == {"source": "snowflake_scored_accounts_csv"}
    assert payload["limit"] == 10
    # apply defaults to False / omitted (CLI suppresses falsy)
    assert payload.get("apply", False) is False


def test_backfill_domains_with_company_ids(monkeypatch) -> None:
    import cli.attio.enrichment as enrichment_mod

    registry = _FakeModalRegistry()
    monkeypatch.setattr(
        enrichment_mod.modal.Function,
        "from_name",
        registry.from_name,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "attio",
            "enrichment",
            "backfill-domains",
            "--company-ids",
            "rec_1, rec_2 ,rec_3",
            "--apply",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = registry.functions["attio_backfill_company_domains"].calls[0]["payload"]
    assert payload["company_ids"] == ["rec_1", "rec_2", "rec_3"]
    assert payload["apply"] is True


def test_backfill_domains_rejects_both_selectors() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "attio",
            "enrichment",
            "backfill-domains",
            "--ext-tam-filter",
            '{"source":"x"}',
            "--company-ids",
            "rec_1",
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.stdout + result.stderr


def test_backfill_domains_requires_a_selector() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["attio", "enrichment", "backfill-domains"],
    )
    assert result.exit_code != 0
    assert "required" in result.stdout + result.stderr


def test_backfill_domains_rejects_empty_filter_combined_with_company_ids() -> None:
    """Regression: empty filter JSON must be rejected before selector routing."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "attio",
            "enrichment",
            "backfill-domains",
            "--ext-tam-filter",
            "{}",
            "--company-ids",
            "rec_1",
        ],
    )
    assert result.exit_code != 0
    assert "non-empty JSON object" in (result.stdout + result.stderr)


def test_backfill_domains_rejects_invalid_ext_tam_filter_json() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "attio",
            "enrichment",
            "backfill-domains",
            "--ext-tam-filter",
            "{not valid json",
        ],
    )
    assert result.exit_code != 0
    assert "invalid" in (result.stdout + result.stderr).lower()


def test_backfill_domains_rejects_non_object_ext_tam_filter() -> None:
    """Regression (roborev): JSON arrays/strings parse cleanly but aren't filter objects."""
    runner = CliRunner()
    for payload in ("[]", '"foo"', "42", "true"):
        result = runner.invoke(
            app,
            [
                "attio",
                "enrichment",
                "backfill-domains",
                "--ext-tam-filter",
                payload,
            ],
        )
        assert result.exit_code != 0, f"payload {payload!r} should be rejected"
        assert "non-empty JSON object" in (result.stdout + result.stderr)


def test_backfill_domains_json_override(monkeypatch) -> None:
    import cli.attio.enrichment as enrichment_mod

    registry = _FakeModalRegistry()
    monkeypatch.setattr(
        enrichment_mod.modal.Function,
        "from_name",
        registry.from_name,
    )

    runner = CliRunner()
    payload_json = json.dumps(
        {
            "ext_tam_filter": {"source": "x"},
            "limit": 25,
            "apply": True,
        },
    )
    result = runner.invoke(
        app,
        [
            "attio",
            "enrichment",
            "backfill-domains",
            "--json",
            payload_json,
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = registry.functions["attio_backfill_company_domains"].calls[0]["payload"]
    assert payload["limit"] == 25
    assert payload["apply"] is True
