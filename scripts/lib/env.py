"""Shared helpers for repo-local command-line scripts.

Keep the execution contract in one place so operators and agents see the same
repo-approved invocation form.
"""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPT_DIR.parent


def infisical_run_example(
    script_relpath: str,
    *,
    env_placeholder: str = "<dev|prod>",
    extra_args: str = "",
) -> str:
    """Return the canonical `infisical run` form for a repo script."""

    suffix = f" {extra_args}" if extra_args else ""
    return (
        'infisical run --projectId "$INFISICAL_PROJECT_ID" '
        '--token "$INFISICAL_TOKEN" '
        f"--env={env_placeholder} -- {script_relpath}{suffix}"
    )


def add_repo_root_to_sys_path() -> None:
    """Insert the repo root into `sys.path` if a script needs local imports."""

    repo_root = str(REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
