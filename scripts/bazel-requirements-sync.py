#!/usr/bin/env -S uv run python
"""Synchronize requirements_bazel.txt from the pinned uv.lock.

Why this exists: Bazel's ``rules_python`` ``pip.parse`` hub consumes a pinned,
hashed requirements file — not a live ``uv.lock``. This script is the single
bridge: it runs ``uv export`` against the committed lock and either writes
``requirements_bazel.txt`` (default) or verifies it has not drifted
(``--check``, the CI gate). ``uv.lock`` stays the source of truth; the Bazel
file is a generated artifact that must never be hand-edited.

Why ``--no-emit-project``: the ``gtm`` package itself is an editable install
under uv. Bazel builds first-party code from source (Task 3+), so the editable
``-e .`` line must never land in the Bazel requirements. Hashes are kept
(exact hashed pins) and the default groups/extras are used —
``--no-hashes``/``--all-extras`` are explicitly out of scope.

No credentials are required: ``uv export`` only reads ``uv.lock`` and never
touches the network, so this script runs without ``infisical run``.

Usage::

    uv run scripts/bazel-requirements-sync.py          # write requirements_bazel.txt
    uv run scripts/bazel-requirements-sync.py --check  # CI drift gate (exit 1 on drift)

Every path is anchored to ``REPO_ROOT`` (derived from this script's own
location), so ``uv run`` from any CWD writes to the repo root — not the
caller's working directory (the ``uv run path/to/script.py``-doesn't-chdir
footgun documented in AGENTS.md).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_FILE = REPO_ROOT / "requirements_bazel.txt"

# The exact ``uv export`` argv that produces the Bazel requirements file.
# ``--no-emit-project`` excludes the editable ``gtm`` package itself (Bazel
# builds first-party code from source). Hashes are kept; the default
# groups/extras are used.
EXPORT_COMMAND: list[str] = ["uv", "export", "--no-emit-project"]


def _run_export() -> str:
    """Run ``uv export`` anchored to ``REPO_ROOT``; return its stdout.

    On subprocess failure the uv stderr is surfaced to the operator (a broken
    lock or missing ``uv`` is loud, not a silent empty file) and the process
    exits non-zero before any file is written.
    """
    result = subprocess.run(  # noqa: S603 -- trusted, fixed argv; no shell
        EXPORT_COMMAND,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"`{' '.join(EXPORT_COMMAND)}` failed with exit {result.returncode}.",
            file=sys.stderr,
        )
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify requirements_bazel.txt matches a fresh export (CI drift gate)",
    )
    args = parser.parse_args(argv)

    generated = _run_export()

    if args.check:
        if not REQUIREMENTS_FILE.exists():
            print(
                "requirements_bazel.txt is missing; generate it with:"
                " uv run scripts/bazel-requirements-sync.py",
                file=sys.stderr,
            )
            return 1
        current = REQUIREMENTS_FILE.read_text(encoding="utf-8")
        if current != generated:
            print(
                "requirements_bazel.txt is out of sync with uv.lock."
                " Regenerate with: uv run scripts/bazel-requirements-sync.py",
                file=sys.stderr,
            )
            return 1
        return 0

    REQUIREMENTS_FILE.write_text(generated, encoding="utf-8")
    print(
        f"requirements_bazel.txt synchronized ({len(generated.splitlines())} lines).",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
