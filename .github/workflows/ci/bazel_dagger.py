# ruff: noqa: INP001, TRY003 -- .github/workflows/ci/ is a workflow-support package.
"""Run impacted Bazel targets inside the ARM64 Dagger trial runner."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
import dagger
from dagger import dag

BASE_IMAGE = "ghcr.io/astral-sh/uv:0.11.29-python3.13-trixie-slim"
RESULT_PATH = "/src/bazel_result"
TRUNK_BAZEL_ACTION_REV = "3e7d4e893f2c4c3c1b16e07f3db8ff3585e4025d"

IMPACTED_VALIDATE_CMD = """
set -euo pipefail
action_dir=/opt/trunk-bazel-action
git clone --quiet https://github.com/trunk-io/bazel-action.git "${action_dir}"
git -C "${action_dir}" checkout --quiet "${TRUNK_BAZEL_ACTION_REV}"

# Execute the same scripts and filters as trunk-io/bazel-action. Dagger owns
# the ARM64 container and cache; the GitHub-hosted action remains responsible
# for uploading the resulting target graph to Trunk MergeGraph.
export DEFAULT_BRANCH=main
export TARGET_BRANCH=main
export PR_BRANCH=HEAD
export WORKSPACE_PATH=/src
export BAZEL_PATH=bazel
source "${action_dir}/src/scripts/prerequisites.sh"
java="$(bazel --output_user_root=/var/cache/bazel/output-user-root info java-home)/bin/java"
export MERGE_INSTANCE_BRANCH_HEAD_SHA="${merge_base_sha}"
export PR_BRANCH_HEAD_SHA="${pr_branch_testing_head_sha}"
export BAZEL_DIFF_CMD="${java} -jar /opt/bazel-diff.jar"
export BAZEL_DIFF_COMMAND_OPTIONS=--config=ci
export CACHE_DIR=/var/cache/bazel/impacted-targets
mkdir --parents "${CACHE_DIR}"
source "${action_dir}/src/scripts/compute_impacted_targets.sh"
export IMPACTED_TARGETS_FILE="${impacted_targets_out}"
export BAZEL_TEST_COMMAND="test --config=ci"
export BAZEL_KIND_FILTER='.+_library|.+_binary|.+_test'
export BAZEL_NEGATIVE_KIND_FILTER='generated file'
export BAZEL_NEGATIVE_SCOPE_FILTER=//external
export BAZEL_NEGATIVE_TAG_FILTER=manual
export CI=true
source "${action_dir}/src/scripts/test_impacted_targets.sh"
""".strip()


def _required_env_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        message = f"{name} must name a prepared host cache path"
        raise ValueError(message)
    return Path(value)


def build_container(
    bazel_binary: Path,
    cache_dir: Path,
    source_dir: Path,
    *,
    diff_jar: Path,
) -> dagger.Container:
    """Build the isolated ARM64 Bazel environment from Namespace-backed caches."""
    source = dag.host().directory(
        str(source_dir),
        exclude=[
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
    container = (
        dag.container(platform=dagger.Platform("linux/arm64"))
        .from_(BASE_IMAGE)
        .with_exec(["bash", "-c", install_system_tools])
        .with_file("/usr/local/bin/bazel", dag.host().file(str(bazel_binary)))
        .with_exec(["chmod", "+x", "/usr/local/bin/bazel"])
        .with_directory("/src", source)
        .with_workdir("/src")
        .with_directory("/var/cache/bazel", dag.host().directory(str(cache_dir)))
    )
    container = container.with_file(
        "/opt/bazel-diff.jar",
        dag.host().file(str(diff_jar)),
    ).with_env_variable(
        "TRUNK_BAZEL_ACTION_REV",
        TRUNK_BAZEL_ACTION_REV,
    )
    return container.with_exec(
        [
            "bash",
            "-c",
            f"({IMPACTED_VALIDATE_CMD}); rc=$?; echo ${{rc}} > {RESULT_PATH}",
        ],
    )


async def main() -> None:
    """Export the persistent cache before returning the impacted-test status."""
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        bazel_binary = _required_env_path("BAZEL_DAGGER_BINARY")
        cache_dir = _required_env_path("BAZEL_DAGGER_CACHE_DIR")
        diff_jar = _required_env_path("BAZEL_DAGGER_DIFF_JAR")
        source_dir = _required_env_path("BAZEL_DAGGER_SOURCE_DIR")
        container = build_container(
            bazel_binary,
            cache_dir,
            source_dir,
            diff_jar=diff_jar,
        )
        await container.sync()
        result = (await container.file(RESULT_PATH).contents()).strip()
        rc = int(result)
        await container.directory("/var/cache/bazel").export(str(cache_dir))
    if rc:
        sys.stderr.write(f"Bazel validation exited {rc}\n")
    raise SystemExit(rc)


if __name__ == "__main__":
    anyio.run(main)
