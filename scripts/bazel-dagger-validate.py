#!/usr/bin/env -S uv run python
# ruff: noqa: N999 -- direct executable scripts use hyphenated filenames.
"""Run the pull-request Bazel/Dagger controller locally against a Git base."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLCHAIN_SCRIPT = REPO_ROOT / "scripts" / "bazel-dagger-toolchain.py"
CONTROLLER = REPO_ROOT / ".github" / "workflows" / "ci" / "bazel_dagger.py"
DEFAULT_BASE = "origin/main"


def _run(command: list[str], *, cwd: Path = REPO_ROOT) -> None:
    """Run a fixed local preparation command and fail on non-zero status."""
    subprocess.run(command, cwd=cwd, check=True)  # noqa: S603,S607 -- fixed local argv.  # nosec B603,B607


def _resolve_commit(ref: str) -> str:
    """Resolve a Git ref to an immutable commit identifier."""
    completed = subprocess.run(  # noqa: S603 -- fixed local Git argv.  # nosec B603,B607
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],  # noqa: S607 -- Git is resolved via PATH.
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _controller_env(
    *,
    source_dir: Path,
    toolchain_dir: Path,
    run_impacted: bool = True,
) -> dict[str, str]:
    """Construct the minimal environment exposed to the Dagger controller."""
    # Keep command lookup and ordinary local Dagger configuration, but do not
    # forward the host environment wholesale: it may contain unrelated
    # credentials that the local controller does not need.
    env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")
        if os.environ.get(name)
    }
    env.update(
        {
            "BAZEL_DAGGER_BINARY": str(toolchain_dir / "bazel"),
            "BAZEL_DAGGER_CACHE_DIR": str(toolchain_dir.parent / "cache"),
            "BAZEL_DAGGER_DIFF_JAR": str(toolchain_dir / "bazel-diff_deploy.jar"),
            "BAZEL_DAGGER_SOURCE_DIR": str(source_dir),
            "BAZEL_RUN_IMPACTED": str(run_impacted).lower(),
            "DAGGER_NO_NAG": "1",
        },
    )
    return env


def run(base: str, *, run_impacted: bool = True) -> int:
    """Validate the current checkout and optionally compare it with a Git base."""
    base_commit = _resolve_commit(base)
    head_commit = _resolve_commit("HEAD")
    toolchain_dir = Path.home() / ".bazel-dagger" / "toolchain"
    cache_dir = toolchain_dir.parent / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    _run(["uv", "run", str(TOOLCHAIN_SCRIPT), "--directory", str(toolchain_dir)])

    scratch_root = REPO_ROOT / "tmp"
    scratch_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bazel-dagger-", dir=scratch_root) as temp:
        source_dir = Path(temp) / "source"
        _run(
            [
                "git",
                "clone",
                "--no-local",
                "--no-checkout",
                "--no-single-branch",
                ".",
                str(source_dir),
            ],
        )
        _run(["git", "checkout", "--detach", head_commit], cwd=source_dir)
        _run(
            ["git", "update-ref", "refs/remotes/origin/main", base_commit],
            cwd=source_dir,
        )
        return subprocess.run(  # noqa: S603,S607 -- fixed controller argv.  # nosec B603,B607
            ["uv", "run", "dagger", "run", "python", str(CONTROLLER)],  # noqa: S607 -- uv is resolved via PATH.
            cwd=REPO_ROOT,
            env=_controller_env(
                source_dir=source_dir,
                toolchain_dir=toolchain_dir,
                run_impacted=run_impacted,
            ),
            check=False,
        ).returncode


def main(argv: list[str] | None = None) -> int:
    """Parse the comparison base and return the controller's exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help="Git ref to compare with HEAD",
    )
    parser.add_argument(
        "--skip-impacted",
        action="store_true",
        help="Run only the full Bazel graph",
    )
    args = parser.parse_args(argv)
    try:
        return run(args.base, run_impacted=not args.skip_impacted)
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        return exc.returncode
    except FileNotFoundError as exc:
        print(f"required command is unavailable: {exc.filename}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
