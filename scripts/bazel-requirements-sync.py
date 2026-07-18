#!/usr/bin/env -S uv run python
"""Project the frozen uv lock into a checked Bazel requirements file.

`requirements_bazel.txt` is a derivative artifact, never a second source of
dependency truth: it is regenerated from `uv.lock` (frozen) and committed so
Bazel's pip layer sees a stable, hashed wheel set without re-running uv at
build time. Drift is detected by regenerating the projection to a buffer and
diffing the committed file against it.

Contract:
  * Project the FROZEN lock only (`--frozen`) so a stale or modified
    `pyproject.toml` can never silently change what Bazel installs.
  * Keep hashes (no `--no-hashes`) and the default groups (no `--no-dev`);
    exclude the project root (`--no-emit-project`) because Bazel builds it from
    source, and never widen with `--all-extras`, credentials, or private-index
    configuration.
  * Invoke uv with an argv list (never a shell string).
  * Write atomically: a uv failure leaves the previously committed good file
    untouched, so a broken export can never half-empty the requirements file.

Usage:
    scripts/bazel-requirements-sync.py            # regenerate requirements_bazel.txt
    scripts/bazel-requirements-sync.py --check     # exit 1 on drift, 0 in sync
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import typer

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_OUTPUT = REPO_ROOT / "requirements_bazel.txt"

# The exact argv fed to `uv export`. Frozen so the projection tracks the
# committed lock rather than a stale pyproject; hashes left intact; the project
# root excluded because Bazel builds the first-party package itself and the
# requirements file must list only third-party wheels. Kept as a list so the
# subprocess call is argv-based (never a shell string) and the contract auditable.
EXPORT_COMMAND: list[str] = ["uv", "export", "--frozen", "--no-emit-project"]


class ExportError(RuntimeError):
    """Raised when the frozen uv projection cannot be produced."""


def render_projection(repo_root: Path) -> str:
    """Return the exact requirements projection uv emits for the frozen lock."""
    result = subprocess.run(
        EXPORT_COMMAND,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ExportError(
            f"uv export failed (exit {result.returncode}) in {repo_root}:\n"
            f"{result.stderr.strip()}",
        )
    return result.stdout


def _write_atomically(path: Path, content: str) -> None:
    """Replace `path` with `content` atomically; a crash never truncates it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        # os.replace already moved the temp file on success; only a pre-replace
        # failure leaves it behind. Never touch the good file here.
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def generate(*, output: Path, repo_root: Path) -> None:
    """Regenerate `output` from the frozen lock; preserve it on export failure."""
    projection = render_projection(repo_root)
    _write_atomically(output, projection)


def is_in_sync(*, output: Path, repo_root: Path) -> bool:
    """True iff the committed `output` matches a fresh frozen projection."""
    if not output.exists():
        return False
    return output.read_text(encoding="utf-8") == render_projection(repo_root)


def main(
    check: bool = typer.Option(
        False,
        "--check",
        help="Exit 1 if requirements_bazel.txt drifted from the frozen lock; "
        "write nothing.",
    ),
    output: Path = typer.Option(
        DEFAULT_OUTPUT,
        "--output",
        help="Path to the checked requirements file.",
    ),
    repo_root: Path = typer.Option(
        REPO_ROOT,
        "--repo-root",
        help="Directory holding pyproject.toml and uv.lock.",
    ),
) -> None:
    if check:
        if not is_in_sync(output=output, repo_root=repo_root):
            typer.echo(
                f"{output} is out of date with the frozen uv lock; "
                "run scripts/bazel-requirements-sync.py to regenerate.",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(f"{output} is in sync with the frozen uv lock.")
        return
    generate(output=output, repo_root=repo_root)
    typer.echo(f"regenerated {output} from the frozen uv lock.")


if __name__ == "__main__":
    typer.run(main)
