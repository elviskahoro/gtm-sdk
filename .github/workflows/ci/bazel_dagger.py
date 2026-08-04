# ruff: noqa: INP001, TRY003 -- .github/workflows/ci/ is a workflow-support package.
"""Run Bazel validation inside the ARM64 Dagger runner.

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

IMPACTED_VALIDATE_CMD = """
set -euo pipefail
base_sha="${BAZEL_DAGGER_BASE_SHA:?missing PR base SHA}"
head_sha="${BAZEL_DAGGER_HEAD_SHA:?missing PR head SHA}"
test "$(git rev-parse HEAD)" = "${head_sha}"
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
    mode: str,
    diff_jar: Path | None = None,
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
    if mode == "full":
        command = VALIDATE_CMD
        container = container.with_exec(["bash", "-c", GIT_INIT_CMD])
    elif mode == "impacted":
        if diff_jar is None:
            raise ValueError("BAZEL_DAGGER_DIFF_JAR must name the verified bazel-diff jar")
        command = IMPACTED_VALIDATE_CMD
        container = (
            container.with_directory("/src/.git", dag.host().directory(".git"))
            .with_file("/opt/bazel-diff.jar", dag.host().file(str(diff_jar)))
            .with_env_variable(
                "BAZEL_DAGGER_BASE_SHA",
                os.environ["BAZEL_DAGGER_BASE_SHA"],
            )
            .with_env_variable(
                "BAZEL_DAGGER_HEAD_SHA",
                os.environ["BAZEL_DAGGER_HEAD_SHA"],
            )
        )
    else:
        raise ValueError(f"unsupported BAZEL_DAGGER_MODE: {mode}")
    return container.with_exec(
        ["bash", "-c", f"({command}); rc=$?; echo ${{rc}} > {RESULT_PATH}"],
    )


async def main() -> None:
    """Export native Bazel JUnit output before returning the real suite status."""
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        mode = os.environ.get("BAZEL_DAGGER_MODE", "full")
        bazel_binary = _required_env_path("BAZEL_DAGGER_BINARY")
        cache_dir = _required_env_path("BAZEL_DAGGER_CACHE_DIR")
        diff_jar = (
            _required_env_path("BAZEL_DAGGER_DIFF_JAR")
            if mode == "impacted"
            else None
        )
        container = build_container(
            bazel_binary,
            cache_dir,
            mode=mode,
            diff_jar=diff_jar,
        )
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
