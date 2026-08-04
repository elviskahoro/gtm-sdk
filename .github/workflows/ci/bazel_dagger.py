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

IMPACTED_VALIDATE_CMD = """
set -euo pipefail
base_sha="${BAZEL_DAGGER_BASE_SHA:?missing PR base SHA}"
# GitHub checks out the PR merge commit; this is the exact revision that the
# GitHub-hosted Trunk action tests against the PR base.
head_sha="$(git rev-parse HEAD)"
java="$(bazel --output_user_root=/var/cache/bazel/output-user-root info java-home)/bin/java"
hash_root=/var/cache/bazel/impacted-targets
mkdir -p "${hash_root}"
for sha in "${base_sha}" "${head_sha}"; do
  git checkout -q "${sha}"
  "${java}" -jar /opt/bazel-diff.jar generate-hashes \
    --bazelPath bazel \
    --workspacePath /src \
    --bazelCommandOptions "--noshow_progress --config=ci" \
    "${hash_root}/${sha}"
done
git checkout -q "${head_sha}"
impacted_targets=/tmp/impacted-targets.txt
"${java}" -jar /opt/bazel-diff.jar get-impacted-targets \
  --startingHashes="${hash_root}/${base_sha}" \
  --finalHashes="${hash_root}/${head_sha}" \
  --workspacePath /src \
  --output="${impacted_targets}"
query_file=/tmp/impacted-target-query.txt
{
  echo "let targets = set("
  sed -e "s/^/'/" -e "s/$/'/" "${impacted_targets}"
  echo ") in"
  echo "let targets = kind('.+_library|.+_binary|.+_test', \\$targets) in"
  echo '$targets'
  echo "- attr('tags', 'manual', \\$targets)"
  echo "- kind('generated file', \\$targets)"
  echo "- filter('//external', \\$targets)"
} > "${query_file}"
filtered_targets=/tmp/filtered-impacted-targets.txt
bazel --output_user_root=/var/cache/bazel/output-user-root query \
  --query_file="${query_file}" > "${filtered_targets}"
if test ! -s "${filtered_targets}"; then
  echo "No testable impacted Bazel targets"
  exit 0
fi
bazel --output_user_root=/var/cache/bazel/output-user-root test --config=ci \
  --target_pattern_file="${filtered_targets}" \
  --repository_cache=/var/cache/bazel/repository \
  --disk_cache=/var/cache/bazel/disk
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
    *,
    diff_jar: Path,
) -> dagger.Container:
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
    container = (
        container.with_directory("/src/.git", dag.host().directory(".git"))
        .with_file("/opt/bazel-diff.jar", dag.host().file(str(diff_jar)))
        .with_env_variable(
            "BAZEL_DAGGER_BASE_SHA",
            os.environ["BAZEL_DAGGER_BASE_SHA"],
        )
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
        container = build_container(
            bazel_binary,
            cache_dir,
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
