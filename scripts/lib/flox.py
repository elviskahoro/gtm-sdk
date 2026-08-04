"""Shared Flox invocation helpers for repo scripts.

Flox is the primary execution environment. Dagger, when requested, only
re-executes the same script inside a prebuilt image made from this environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def flox_activate_prefix(repo_root: Path) -> list[str]:
    """The ``flox activate`` argv that wraps each Flox-executor step.

    Takes ``repo_root`` explicitly rather than resolving it from this
    module's own ``__file__`` (e.g. via a shared ``scripts.lib.env.REPO_ROOT``
    constant): the CI pytest pipeline pre-imports ``scripts.lib`` from a
    second, stable checkout at ``/opt/gtm-sdk`` (see
    ``.github/workflows/ci/pytest_dagger.py``'s ``sitecustomize.py`` shim),
    which is a different path than the actual repo under test (``/src``).
    A module-level ``REPO_ROOT`` computed inside ``scripts/lib/`` would
    silently resolve to that stable shim location instead of the caller's
    real checkout. Each caller already computes its own correct
    ``REPO_ROOT`` from its own ``__file__`` and passes it in here.

    ``--mode run`` (not ``dev``): flox refuses a dev-mode activation while
    another shell holds a run-mode one on the same env, and the two modes
    resolve different Nix store paths.
    """
    return ["flox", "activate", "--dir", str(repo_root), "--mode", "run", "--"]


def in_flox_env() -> bool:
    """Return whether the current process was launched by an activated Flox env."""
    return bool(os.environ.get("FLOX_ENV"))


def run(
    argv: list[str],
    *,
    repo_root: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
    clear_env: bool = False,
) -> str | None:
    """Run one command in the repo's activated Flox environment."""
    child_env = {} if clear_env else dict(os.environ)
    if env is not None:
        child_env.update(env)
    proc = subprocess.run(  # noqa: S603
        [*flox_activate_prefix(repo_root), *argv],
        cwd=repo_root,
        env=child_env,
        capture_output=capture,
        text=True,
        check=True,
    )
    return proc.stdout if capture else None


def preflight(repo_root: Path, tools: tuple[str, ...]) -> str:
    """Validate Flox activation and return its resolved environment path."""
    if shutil.which("flox") is None:
        msg = "flox is required for the primary execution path"
        raise RuntimeError(msg)
    proc = subprocess.run(  # noqa: S603
        [*flox_activate_prefix(repo_root), "sh", "-c", 'printf %s "$FLOX_ENV"'],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    flox_env = proc.stdout.strip()
    if not flox_env:
        msg = "flox activation did not set FLOX_ENV"
        raise RuntimeError(msg)
    missing = [tool for tool in tools if not (Path(flox_env) / "bin" / tool).exists()]
    if missing:
        msg = f"Flox environment {flox_env} is missing required tools: {', '.join(missing)}"
        raise RuntimeError(msg)
    return flox_env
