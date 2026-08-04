# ruff: noqa: INP001, PLR2004, S310, TRY003 -- .github/workflows/ci/ is a workflow-support package.
"""Run impacted Bazel targets inside the ARM64 Dagger runner."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import anyio
import dagger
from dagger import dag

BASE_IMAGE = "ghcr.io/astral-sh/uv:0.11.29-python3.13-trixie-slim"
RESULT_PATH = "/src/bazel_result"
ANALYTICS_RESULT_PATH = "/src/trunk_analytics_result"
BEP_PATH = "/src/build_events.json"
IMPACTED_TARGETS_PATH = "/src/impacted_targets.txt"
CHANGED_PATHS_PATH = "/src/changed_paths.txt"
TRUNK_BAZEL_ACTION_REV = "3e7d4e893f2c4c3c1b16e07f3db8ff3585e4025d"
TRUNK_ANALYTICS_VERSION = "0.15.1"
TRUNK_ANALYTICS_SHA256 = (
    "3368cf0e33689db773d77fed76bff8d29d931089c09d0f1395956db849b87462"
)
TRUNK_ANALYTICS_URL = (
    "https://github.com/trunk-io/analytics-cli/releases/download/"
    f"{TRUNK_ANALYTICS_VERSION}/trunk-analytics-cli-aarch64-unknown-linux.tar.gz"
)
IMPACTED_TARGETS_URL = "https://api.trunk.io:443/v1/setImpactedTargets"
MAX_IMPACTED_TARGETS_BODY_BYTES = 20_000_000

# Bazel writes the Build Event Protocol stream here, and Trunk Flaky Tests reads
# it to locate each target's test.xml. The paths inside the BEP point at the
# container's Bazel output base, so the upload has to happen in the container --
# exporting build_events.json to the host would leave every report path dangling.
# Forwarded to the container so the analytics CLI receives GitHub metadata even
# though it runs inside Dagger rather than directly in the Actions runner.
# TRUNK_API_TOKEN is passed separately, as a Dagger secret.
UPLOAD_ENV_VARS = (
    "CUSTOM",
    "JOB_URL",
    "JOB_NAME",
    "COMMIT_SHA",
    "COMMIT_BRANCH",
)

ALL_IMPACTING_PATHS = frozenset(
    {
        ".trunk/trunk.yaml",
        "MODULE.bazel",
        "pyproject.toml",
        "uv.lock",
        "requirements_bazel.txt",
        "scripts/bazel-requirements-sync.py",
    },
)

IMPACTED_VALIDATE_CMD = """
set -euo pipefail
action_dir=/opt/trunk-bazel-action
git clone --quiet https://github.com/trunk-io/bazel-action.git "${action_dir}"
git -C "${action_dir}" checkout --quiet "${TRUNK_BAZEL_ACTION_REV}"
# The impacted-targets helper cleans the workspace after each revision switch.
# Regenerate the untracked Bazel input after every such cleanup so each Bazel
# query uses the uv.lock belonging to the revision currently checked out.
compute_script="${action_dir}/src/scripts/compute_impacted_targets.sh"
sed -i '/git clean -dfx -f/a rm -rf .venv && uv run scripts/bazel-requirements-sync.py' "${compute_script}"

# Execute the same scripts and filters as trunk-io/bazel-action. Dagger owns
# the ARM64 container and cache; this controller uploads the resulting target
# graph to Trunk MergeGraph after the container returns it.
export DEFAULT_BRANCH=main
export TARGET_BRANCH=main
export PR_BRANCH=HEAD
export WORKSPACE_PATH=/src
export BAZEL_PATH=bazel
export BAZEL_STARTUP_OPTIONS=--output_user_root=/var/cache/bazel/output-user-root
source "${action_dir}/src/scripts/prerequisites.sh"
uv run scripts/bazel-requirements-sync.py
test -s requirements_bazel.txt
java="$(bazel ${BAZEL_STARTUP_OPTIONS} info java-home)/bin/java"
export MERGE_INSTANCE_BRANCH_HEAD_SHA="${merge_base_sha}"
export PR_BRANCH_HEAD_SHA="${pr_branch_testing_head_sha}"
export BAZEL_DIFF_CMD="${java} -jar /opt/bazel-diff.jar"
export BAZEL_DIFF_COMMAND_OPTIONS=--config=ci
export CACHE_DIR=/var/cache/bazel/impacted-targets
mkdir --parents "${CACHE_DIR}"
source "${action_dir}/src/scripts/compute_impacted_targets.sh"
export IMPACTED_TARGETS_FILE="${impacted_targets_out}"
cp "${IMPACTED_TARGETS_FILE}" /src/impacted_targets.txt
git diff --name-only "${merge_base_sha}" "${pr_branch_testing_head_sha}" \
  > /src/changed_paths.txt
export BAZEL_TEST_COMMAND="test --config=ci --nobuild_event_json_file_path_conversion --build_event_json_file=/src/build_events.json"
export BAZEL_KIND_FILTER='.+_library|.+_binary|.+_test'
export BAZEL_SCOPE_FILTER=""
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


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        message = f"{name} must be set for a Trunk upload"
        raise ValueError(message)
    return value


def _impacts_all(changed_paths: list[str]) -> bool:
    return any(
        path in ALL_IMPACTING_PATHS or path.startswith(".github/workflows/")
        for path in changed_paths
    )


def _impacted_targets_payload(
    *,
    repository: str,
    pr_number: str,
    pr_sha: str,
    target_branch: str,
    targets: list[str],
    changed_paths: list[str],
) -> bytes:
    owner, separator, name = repository.partition("/")
    if not separator or not owner or not name:
        message = "TRUNK_REPOSITORY must be an owner/name GitHub repository"
        raise ValueError(message)

    impacted_targets: list[str] | str = (
        "ALL" if _impacts_all(changed_paths) else targets
    )
    payload = {
        "repo": {"host": "github.com", "owner": owner, "name": name},
        "pr": {"number": int(pr_number), "sha": pr_sha},
        "targetBranch": target_branch,
        "impactedTargets": impacted_targets,
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    if len(encoded) > MAX_IMPACTED_TARGETS_BODY_BYTES and impacted_targets != "ALL":
        payload["impactedTargets"] = "ALL"
        encoded = json.dumps(payload, separators=(",", ":")).encode()
    return encoded


def _post_impacted_targets(token: str, payload: bytes) -> None:
    request = Request(
        IMPACTED_TARGETS_URL,
        data=payload,
        headers={"Content-Type": "application/json", "x-api-token": token},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 -- fixed Trunk API URL.
            if not 200 <= response.status < 300:
                message = (
                    f"Trunk impacted-target upload returned HTTP {response.status}"
                )
                raise RuntimeError(message)
    except HTTPError as exc:
        message = f"Trunk impacted-target upload returned HTTP {exc.code}"
        raise RuntimeError(message) from exc


def build_container(
    bazel_binary: Path,
    cache_dir: Path,
    source_dir: Path,
    *,
    diff_jar: Path,
    trunk_api_token: str,
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
apt-get install --yes --no-install-recommends build-essential git unzip
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
    for name in UPLOAD_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            container = container.with_env_variable(name, value)
    if trunk_api_token:
        token_secret = dag.set_secret("trunk-api-token", trunk_api_token)
        container = container.with_secret_variable("TRUNK_API_TOKEN", token_secret)

    container = container.with_exec(
        [
            "bash",
            "-c",
            f"({IMPACTED_VALIDATE_CMD}); rc=$?; echo ${{rc}} > {RESULT_PATH}",
        ],
    )
    if not trunk_api_token:
        return container

    upload_command = f"""
set -uo pipefail
rc=0
if [ ! -s {BEP_PATH} ]; then
  echo "No Bazel test results were produced; skipping Trunk Flaky Tests upload."
else
  {{
    curl --fail --location --silent --show-error {TRUNK_ANALYTICS_URL} \
      | tee /tmp/trunk-analytics-cli.tar.gz \
      | tar --extract --gzip --file - --directory /usr/local/bin
    echo "{TRUNK_ANALYTICS_SHA256}  /tmp/trunk-analytics-cli.tar.gz" | sha256sum --check
    chmod +x /usr/local/bin/trunk-analytics-cli
    trunk-analytics-cli validate --bazel-bep-path={BEP_PATH}
    trunk-analytics-cli upload --bazel-bep-path={BEP_PATH} \
      --org-url-slug sanhedrin \
      --variant bazel \
      --use-bazel-target-for-codeowners \
      --allow-empty-test-results=false \
      --token "$TRUNK_API_TOKEN"
  }} || rc=$?
fi
echo "${{rc}}" > {ANALYTICS_RESULT_PATH}
exit 0
""".strip()
    return container.with_exec(["bash", "-c", upload_command])


async def main() -> None:
    """Export the persistent cache before returning the impacted-test status."""
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        bazel_binary = _required_env_path("BAZEL_DAGGER_BINARY")
        cache_dir = _required_env_path("BAZEL_DAGGER_CACHE_DIR")
        diff_jar = _required_env_path("BAZEL_DAGGER_DIFF_JAR")
        source_dir = _required_env_path("BAZEL_DAGGER_SOURCE_DIR")
        trunk_api_token = os.environ.get("TRUNK_API_TOKEN", "").strip()
        container = build_container(
            bazel_binary,
            cache_dir,
            source_dir,
            diff_jar=diff_jar,
            trunk_api_token=trunk_api_token,
        )
        await container.sync()
        result = (await container.file(RESULT_PATH).contents()).strip()
        rc = int(result)
        analytics_rc = 0
        if trunk_api_token:
            analytics_result = (
                await container.file(ANALYTICS_RESULT_PATH).contents()
            ).strip()
            analytics_rc = int(analytics_result)
            targets = (
                await container.file(IMPACTED_TARGETS_PATH).contents()
            ).splitlines()
            changed_paths = (
                await container.file(CHANGED_PATHS_PATH).contents()
            ).splitlines()
            payload = _impacted_targets_payload(
                repository=_required_env("TRUNK_REPOSITORY"),
                pr_number=_required_env("TRUNK_PR_NUMBER"),
                pr_sha=_required_env("TRUNK_PR_HEAD_SHA"),
                target_branch=_required_env("TRUNK_PR_BASE_REF"),
                targets=targets,
                changed_paths=changed_paths,
            )
            await anyio.to_thread.run_sync(
                _post_impacted_targets,
                trunk_api_token,
                payload,
            )
        await container.directory("/var/cache/bazel").export(str(cache_dir))
    if rc:
        sys.stderr.write(f"Bazel validation exited {rc}\n")
    if analytics_rc:
        sys.stderr.write(f"Trunk Flaky Tests upload exited {analytics_rc}\n")
    raise SystemExit(rc or analytics_rc)


if __name__ == "__main__":
    anyio.run(main)
