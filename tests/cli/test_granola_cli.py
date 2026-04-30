from __future__ import annotations

import json

from typer.testing import CliRunner

from cli.main import app


def test_granola_group_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["granola", "--help"])
    assert result.exit_code == 0
    assert "export" in result.stdout


def test_granola_export_help_contains_source_and_output() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["granola", "export", "--help"])
    assert result.exit_code == 0
    assert "--source" in result.stdout
    assert "--output" in result.stdout


def test_granola_source_api_without_key_fails() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["granola", "export", "--source", "api"])
    assert result.exit_code == 1
    assert "GRANOLA_API_KEY" in result.stderr


def test_granola_export_success_outputs_json(monkeypatch, tmp_path) -> None:
    from libs.granola.models import ExportRunResult

    def _fake_run(*_args, **_kwargs) -> ExportRunResult:
        return ExportRunResult(
            source="hybrid",
            processed=2,
            written=1,
            skipped=1,
            errors=0,
            manifest_path=str(manifest_path),
            state_path=str(state_path),
        )

    import cli.granola.export as export_cli

    manifest_path = tmp_path / "manifest.jsonl"
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(export_cli, "run_export", _fake_run)

    runner = CliRunner()
    result = runner.invoke(app, ["granola", "export"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source"] == "hybrid"
    assert payload["processed"] == 2


def test_granola_export_preflight_error_prints_stderr(monkeypatch) -> None:
    from libs.granola.errors import ConfigError

    def _fake_run(*_args, **_kwargs):
        raise ConfigError("bad preflight")

    import cli.granola.export as export_cli

    monkeypatch.setattr(export_cli, "run_export", _fake_run)

    runner = CliRunner()
    result = runner.invoke(app, ["granola", "export"])
    assert result.exit_code == 1
    assert "bad preflight" in result.stderr
