# ruff: noqa: INP001, TRY003 -- .github/workflows/ci/ is a workflow-support package.
"""Run the full Bazel validation suite inside the ARM64 Dagger runner.

This is deliberately a parallel trial runner for #488. It mirrors the
GitHub-hosted ``Full Bazel suite`` checks, while consuming Namespace-backed
Bazel caches. The workflow exports Bazel's native JUnit files so Trunk can
compare its analytics with the existing pytest uploader.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
import dagger
from dagger import dag

BASE_IMAGE = "ghcr.io/astral-sh/uv:0.11.29-python3.13-trixie-slim"
BAZEL_VERSION = "8.7.0"
JUNIT_HOST_PATH = "bazel-testlogs"
RESULT_PATH = "/src/bazel_result"
GIT_INIT_CMD = (
    "git init -q && "
    "git -c user.email=ci@example.com -c user.name=ci "
    "  -c commit.gpgsign=false add -A && "
    "git -c user.email=ci@example.com -c user.name=ci "
    "  -c commit.gpgsign=false commit -q -m 'dagger throwaway' --no-verify"
)

# Keep this command shell-only so its exit status can be captured after all
# runnable checks. ``bazel test`` is intentionally last: even when a prior
# validation fails, failures retain their original diagnostic rather than being
# hidden behind an attempted test-report export.
VALIDATE_CMD = f"""
set -euo pipefail
test "$(bazel --version)" = "bazel {BAZEL_VERSION}"
bazel --output_user_root=/var/cache/bazel/output-user-root run @python_3_13_13//:python3 -- --version 2>&1 | tee /tmp/hermetic-python-version.log
test "$(tail -n 1 /tmp/hermetic-python-version.log)" = "Python 3.13.13"
scripts/bazel-requirements-sync.py --check
bazel --output_user_root=/var/cache/bazel/output-user-root test //... --config=ci --repository_cache=/var/cache/bazel/repository --disk_cache=/var/cache/bazel/disk
bazel --output_user_root=/var/cache/bazel/output-user-root run //:gazelle --repository_cache=/var/cache/bazel/repository --disk_cache=/var/cache/bazel/disk
# Gazelle can create a new BUILD file. Intent-to-add makes that untracked file
# visible to the following diff without changing the throwaway commit contents.
git add --intent-to-add -- ':(glob)**/BUILD' ':(glob)**/BUILD.bazel'
git diff --exit-code -- MODULE.bazel.lock BUILD.bazel ':(glob)**/BUILD' ':(glob)**/BUILD.bazel'
""".strip()


def _required_env_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        message = f"{name} must name a prepared host cache path"
        raise ValueError(message)
    return Path(value)


def build_container(bazel_binary: Path, cache_dir: Path) -> dagger.Container:
    """Build the isolated ARM64 Bazel environment from Namespace-backed caches."""
    source = dag.host().directory(
        ".",
        exclude=[
            ".git",
            ".venv",
            ".pytest_cache",
            ".ruff_cache",
            "bazel-*",
            "tmp",
            "worktrees",
        ],
    )
    install_system_tools = """
apt-get update
apt-get install --yes --no-install-recommends git unzip
""".strip()
    return (
        dag.container(platform=dagger.Platform("linux/arm64"))
        .from_(BASE_IMAGE)
        .with_exec(["bash", "-c", install_system_tools])
        .with_file("/usr/local/bin/bazel", dag.host().file(str(bazel_binary)))
        .with_exec(["chmod", "+x", "/usr/local/bin/bazel"])
        .with_directory("/src", source)
        .with_workdir("/src")
        .with_directory("/var/cache/bazel", dag.host().directory(str(cache_dir)))
        .with_exec(["bash", "-c", GIT_INIT_CMD])
        .with_exec(
            [
                "bash",
                "-c",
                f"({VALIDATE_CMD}); rc=$?; echo ${{rc}} > {RESULT_PATH}",
            ],
        )
    )


async def main() -> None:
    """Export native Bazel JUnit output before returning the real suite status."""
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        bazel_binary = _required_env_path("BAZEL_DAGGER_BINARY")
        cache_dir = _required_env_path("BAZEL_DAGGER_CACHE_DIR")
        container = build_container(bazel_binary, cache_dir)
        await container.sync()
        result = (await container.file(RESULT_PATH).contents()).strip()
        rc = int(result)
        await container.directory("/var/cache/bazel").export(str(cache_dir))
        try:
            await container.directory("/src/bazel-testlogs").export(JUNIT_HOST_PATH)
        except dagger.DaggerError as exc:
            if rc == 0:
                raise
            sys.stderr.write(f"warning: could not export Bazel JUnit results: {exc}\n")
    if rc:
        sys.stderr.write(f"Bazel validation exited {rc}\n")
    raise SystemExit(rc)


if __name__ == "__main__":
    anyio.run(main)
