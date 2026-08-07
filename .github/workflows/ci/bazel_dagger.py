# ruff: noqa: INP001, PLR2004, S310, TRY003 -- .github/workflows/ci/ is a workflow-support package.
"""Run Bazel validation inside the ARM64 Dagger runner."""

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

BASE_IMAGE = "ghcr.io/elviskahoro/gtm-sdk/bazel-ci@sha256:6e45b2a27d374aa0c3ba8908788008816cb881f9194ab27f7f0aab4f5cf636ec"


FULL_RESULT_PATH = "/src/full_bazel_result"
IMPACTED_RESULT_PATH = "/src/impacted_bazel_result"
ANALYTICS_RESULT_PATH = "/src/trunk_analytics_result"
FULL_BEP_PATH = "/src/full_build_events.json"
IMPACTED_BEP_PATH = "/src/impacted_build_events.json"
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
# The helper switches revisions after cleaning. Force those switches so the
# temporary historical BUILD repair below cannot block the next checkout.
sed -i 's/git checkout -q /git checkout -q -f /g' "${compute_script}"
sed -i '/git clean -dfx -f/a rm -rf .venv && uv run scripts/bazel-requirements-sync.py' "${compute_script}"
# The current base revision still has the removed Dockerfile globs in its Bazel
# package. Repair only that known historical tree; future revisions retain any
# intentionally added workflow inputs.
sed -i '/git clean -dfx -f/a if test "$(git rev-parse HEAD)" = "42994092b8b40711573f1111b9f34c742c9a371d" && test -f .github/workflows/ci/BUILD.bazel; then sed -i -e "/Dockerfile/d" -e "/dockerignore/d" .github/workflows/ci/BUILD.bazel; fi' "${compute_script}"
# Execute the same scripts and filters as trunk-io/bazel-action. Dagger owns
# the ARM64 container and cache; this controller uploads the resulting target
# graph to Trunk MergeGraph after the container returns it.
export DEFAULT_BRANCH=main
export TARGET_BRANCH=main
export PR_BRANCH=HEAD
export WORKSPACE_PATH=/src
export BAZEL_PATH=bazel
export BAZEL_STARTUP_OPTIONS=--output_user_root=/tmp/bazel-output-user-root
if [ -n "${BAZEL_DIFF_BASE_SHA:-}" ]; then
  git update-ref refs/remotes/origin/main "${BAZEL_DIFF_BASE_SHA}"
fi
source "${action_dir}/src/scripts/prerequisites.sh"
uv run scripts/bazel-requirements-sync.py
test -s requirements_bazel.txt
java="$(bazel ${BAZEL_STARTUP_OPTIONS} info java-home)/bin/java"
export MERGE_INSTANCE_BRANCH_HEAD_SHA="${merge_base_sha}"
export PR_BRANCH_HEAD_SHA="${pr_branch_testing_head_sha}"
export BAZEL_DIFF_CMD="${java} -jar /opt/bazel-diff.jar"
export BAZEL_DIFF_COMMAND_OPTIONS="--config=ci --incompatible_disallow_empty_glob=false"
export CACHE_DIR=/var/cache/bazel/impacted-targets
mkdir --parents "${CACHE_DIR}"
source "${action_dir}/src/scripts/compute_impacted_targets.sh"
export IMPACTED_TARGETS_FILE="${impacted_targets_out}"
cp "${IMPACTED_TARGETS_FILE}" /src/impacted_targets.txt
git diff --name-only "${merge_base_sha}" "${pr_branch_testing_head_sha}" \
  > /src/changed_paths.txt
export BAZEL_TEST_COMMAND="test --config=ci --disk_cache=/var/cache/bazel/disk-cache --repository_cache=/var/cache/bazel/repository-cache --test_tag_filters=-manual --nobuild_event_json_file_path_conversion --build_event_json_file=/src/impacted_build_events.json"
export BAZEL_KIND_FILTER='.+_library|.+_binary|.+_test'
export BAZEL_SCOPE_FILTER=""
export BAZEL_NEGATIVE_KIND_FILTER='generated file'
export BAZEL_NEGATIVE_SCOPE_FILTER=//external
export BAZEL_NEGATIVE_TAG_FILTER=manual
export CI=true
source "${action_dir}/src/scripts/test_impacted_targets.sh"
""".strip()

FULL_VALIDATE_CMD = """
set -euo pipefail
export HYPOTHESIS_PROFILE=ci
uv run scripts/bazel-requirements-sync.py
test -s requirements_bazel.txt
bazel --output_user_root=/tmp/bazel-output-user-root test //... \
  --config=ci \
  --disk_cache=/var/cache/bazel/disk-cache \
  --repository_cache=/var/cache/bazel/repository-cache \
  --test_tag_filters=-manual \
  --nobuild_event_json_file_path_conversion \
  --build_event_json_file=/src/full_build_events.json
""".strip()

COMBINED_VALIDATE_CMD = f"""
set -u
full_rc=0
if [ "${{BAZEL_RUN_FULL:-false}}" = "true" ]; then
  set +e
  ({FULL_VALIDATE_CMD})
  full_rc=$?
  set -e
fi
impacted_rc=0
if [ "${{BAZEL_RUN_IMPACTED:-false}}" = "true" ]; then
  set +e
  ({IMPACTED_VALIDATE_CMD})
  impacted_rc=$?
  set -e
fi
printf '%s\\n' "$full_rc" > {FULL_RESULT_PATH}
printf '%s\\n' "$impacted_rc" > {IMPACTED_RESULT_PATH}
exit 0
""".strip()


def _required_env_path(name: str) -> Path:
    """Read a required host path used to mount a prepared CI cache."""
    value = os.environ.get(name, "").strip()
    if not value:
        message = f"{name} must name a prepared host cache path"
        raise ValueError(message)
    return Path(value)


def _required_env(name: str) -> str:
    """Reject blank metadata before it can authorize a Trunk upload."""
    value = os.environ.get(name, "").strip()
    if not value:
        message = f"{name} must be set for a Trunk upload"
        raise ValueError(message)
    return value


def _impacts_all(changed_paths: list[str]) -> bool:
    """Return whether a changed path invalidates target-level selection."""
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
    """Encode the impacted-target report expected by the Trunk API."""
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
    """Upload an impacted-target report and reject non-success responses."""
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
    ghcr_token: str,
    run_impacted: bool,
) -> dagger.Container:
    """Build the isolated ARM64 Bazel environment from persistent caches."""
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
    uv_cache_dir = cache_dir.parent / "uv-cache"
    container = (
        dag.container(platform=dagger.Platform("linux/arm64"))
        .from_(BASE_IMAGE)
        .with_file("/usr/local/bin/bazel", dag.host().file(str(bazel_binary)))
        .with_exec(["chmod", "+x", "/usr/local/bin/bazel"])
        .with_directory("/src", source)
        .with_workdir("/src")
        .with_directory("/var/cache/bazel", dag.host().directory(str(cache_dir)))
        .with_directory("/var/cache/uv", dag.host().directory(str(uv_cache_dir)))
        .with_env_variable("UV_CACHE_DIR", "/var/cache/uv")
    )
    if ghcr_token:
        container = container.with_registry_auth(
            "ghcr.io",
            "github-actions",
            dag.set_secret("ghcr-token", ghcr_token),
        )
    if run_impacted:
        container = container.with_file(
            "/opt/bazel-diff.jar",
            dag.host().file(str(diff_jar)),
        ).with_env_variable(
            "TRUNK_BAZEL_ACTION_REV",
            TRUNK_BAZEL_ACTION_REV,
        )
    container = container.with_env_variable(
        "BAZEL_RUN_IMPACTED",
        str(run_impacted).lower(),
    ).with_env_variable(
        "BAZEL_RUN_FULL",
        os.environ.get("BAZEL_RUN_FULL", "false").strip().lower(),
    )
    diff_base_sha = os.environ.get("BAZEL_DIFF_BASE_SHA", "").strip()
    if diff_base_sha:
        container = container.with_env_variable("BAZEL_DIFF_BASE_SHA", diff_base_sha)
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
            COMBINED_VALIDATE_CMD,
        ],
    )
    # Bazel's external repositories can create read-only output directories
    # (notably rules_go's generated stdlib tree). Dagger exports the cache as a
    # normal host directory after the container exits, so normalize the copied
    # cache permissions before that export or a green Bazel run can still fail
    # while materializing the persistent host cache.
    normalize_cache_permissions = (
        "find /var/cache/bazel -type d -exec chmod u+rwx {} +; "
        "find /var/cache/bazel -type f -exec chmod u+rw {} +"
    )
    if not trunk_api_token:
        return container.with_exec(["bash", "-c", normalize_cache_permissions])

    upload_command = f"""
set -uo pipefail
rc=0
if [ ! -s {FULL_BEP_PATH} ] && [ ! -s {IMPACTED_BEP_PATH} ]; then
  echo "No Bazel test results were produced; skipping Trunk Flaky Tests upload."
else
  {{
    curl --fail --location --silent --show-error {TRUNK_ANALYTICS_URL} \
      --output /tmp/trunk-analytics-cli.tar.gz \
    && echo "{TRUNK_ANALYTICS_SHA256}  /tmp/trunk-analytics-cli.tar.gz" | sha256sum --check \
    && tar --extract --gzip --file /tmp/trunk-analytics-cli.tar.gz --directory /usr/local/bin \
    && chmod +x /usr/local/bin/trunk-analytics-cli
  }} || rc=$?
  if [ "$rc" -eq 0 ]; then
    for bep in {FULL_BEP_PATH} {IMPACTED_BEP_PATH}; do
      if [ -s "$bep" ]; then
        trunk-analytics-cli validate --bazel-bep-path="$bep" || {{ rc=$?; continue; }}
        trunk-analytics-cli upload --bazel-bep-path="$bep" \
          --org-url-slug sanhedrin \
          --variant bazel \
          --use-bazel-target-for-codeowners \
          --allow-empty-test-results=false \
          --token "$TRUNK_API_TOKEN" || rc=$?
      fi
    done
  fi
fi
echo "${{rc}}" > {ANALYTICS_RESULT_PATH}
exit 0
""".strip()
    return container.with_exec(
        ["bash", "-c", upload_command],
    ).with_exec(["bash", "-c", normalize_cache_permissions])


async def main() -> None:
    """Export the persistent cache before returning the Bazel status."""
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        bazel_binary = _required_env_path("BAZEL_DAGGER_BINARY")
        cache_dir = _required_env_path("BAZEL_DAGGER_CACHE_DIR")
        uv_cache_dir = cache_dir.parent / "uv-cache"
        source_dir = _required_env_path("BAZEL_DAGGER_SOURCE_DIR")
        trunk_api_token = os.environ.get("TRUNK_API_TOKEN", "").strip()
        ghcr_token = os.environ.get("GHCR_TOKEN", "").strip()
        run_impacted = os.environ.get("BAZEL_RUN_IMPACTED", "false").strip()
        if run_impacted not in {"true", "false"}:
            message = "BAZEL_RUN_IMPACTED must be 'true' or 'false'"
            raise ValueError(message)
        run_impacted_bool = run_impacted == "true"
        run_full = os.environ.get("BAZEL_RUN_FULL", "false").strip()
        if run_full not in {"true", "false"}:
            message = "BAZEL_RUN_FULL must be 'true' or 'false'"
            raise ValueError(message)
        diff_jar = (
            _required_env_path("BAZEL_DAGGER_DIFF_JAR")
            if run_impacted_bool
            else Path("/dev/null")
        )
        container = build_container(
            bazel_binary,
            cache_dir,
            source_dir,
            diff_jar=diff_jar,
            trunk_api_token=trunk_api_token,
            ghcr_token=ghcr_token,
            run_impacted=run_impacted_bool,
        )
        await container.sync()
        full_rc = int((await container.file(FULL_RESULT_PATH).contents()).strip())
        impacted_rc = int(
            (await container.file(IMPACTED_RESULT_PATH).contents()).strip(),
        )
        rc = full_rc or impacted_rc
        analytics_rc = 0
        if trunk_api_token:
            analytics_result = (
                await container.file(ANALYTICS_RESULT_PATH).contents()
            ).strip()
            analytics_rc = int(analytics_result)
            has_pr_metadata = all(
                os.environ.get(name, "").strip()
                for name in (
                    "TRUNK_REPOSITORY",
                    "TRUNK_PR_NUMBER",
                    "TRUNK_PR_HEAD_SHA",
                    "TRUNK_PR_BASE_REF",
                )
            )
            if run_impacted_bool and has_pr_metadata:
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
        if os.environ.get("BAZEL_CACHE_WRITE", "false").strip().lower() == "true":
            await container.directory("/var/cache/bazel").export(str(cache_dir))
            await container.directory("/var/cache/uv").export(str(uv_cache_dir))
    if rc:
        sys.stderr.write(f"Bazel validation exited {rc}\n")
    if analytics_rc:
        sys.stderr.write(f"Trunk Flaky Tests upload exited {analytics_rc}\n")
    # Analytics is auxiliary for the canonical full-suite gate; a transient
    # Trunk outage must not turn a passing test run red. Test execution failures
    # from either the full or impacted Bazel invocation remain required failures.
    raise SystemExit(rc)


if __name__ == "__main__":
    anyio.run(main)
