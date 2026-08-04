#!/usr/bin/env -S uv run python
# ruff: noqa: N999
from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import click
import typer

if TYPE_CHECKING:
    from collections.abc import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
OUTPUT = REPO_ROOT / "requirements_bazel.txt"
EXPORT_COMMAND = (
    "uv",
    "export",
    "--frozen",
    "--format",
    "requirements.txt",
    "--no-default-groups",
    "--group",
    "dev",
    "--no-emit-workspace",
    "--no-header",
    "--no-annotate",
)

app = typer.Typer()


def render() -> str:
    completed = subprocess.run(  # noqa: S603,S607 -- fixed uv argv, shell disabled.
        list(EXPORT_COMMAND),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.rstrip("\n") + "\n"


def check(expected: str) -> int:
    actual = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
    if actual == expected:
        return 0
    print(
        "".join(
            difflib.unified_diff(
                actual.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=str(OUTPUT),
                tofile="uv.lock export",
            ),
        ),
        end="",
    )
    return 1


@app.command()
def sync(
    check_mode: bool = typer.Option(  # noqa: FBT001,FBT002
        False,  # noqa: FBT003
        "--check",
        help="Check mode",
    ),
) -> None:
    try:
        expected = render()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc

    if check_mode:
        raise SystemExit(check(expected))

    OUTPUT.write_text(expected, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        app(args=argv, standalone_mode=False)
    except click.ClickException as exc:
        typer.echo(exc.format_message(), err=True)
        return exc.exit_code
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
