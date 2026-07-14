from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import click


def _load_generator() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "scripts" / "docs-cli_reference-generate.py"
    spec = importlib.util.spec_from_file_location("docs_cli_reference_generate", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_page_scopes_json_override_to_commands_with_option() -> None:
    generator = _load_generator()

    @click.command()
    @click.option("--json", "json_input")
    def accepts_json(json_input: str | None) -> None:
        """Structured command."""

    @click.command()
    def plain_text() -> None:
        """Plain command."""

    group = click.Group(help="Mixed commands.")
    group.add_command(accepts_json, "accepts-json")
    group.add_command(plain_text, "plain-text")

    page = generator._subapp_page("sample", group)

    assert "Commands whose option table includes `--json`" in page
    assert "commands without that option do not" in page
    assert "Every command follows" not in page


def test_generated_page_omits_json_override_when_no_command_supports_it() -> None:
    generator = _load_generator()

    @click.command()
    def plain_text() -> None:
        """Plain command."""

    group = click.Group(help="Plain commands.")
    group.add_command(plain_text, "plain-text")

    page = generator._subapp_page("sample", group)

    assert "`--json`" not in page
    assert "Every command follows" not in page
    assert "source of truth for supported options and output behavior" in page
