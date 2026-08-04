# ruff: noqa: INP001, S101, S105 -- workflow tests are standalone and assertion-based.
"""Static contracts for the single ARM64 Dagger Bazel test workflow."""

from pathlib import Path
from typing import Any

import yaml

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests-bazel.yml"
PIPELINE = REPO_ROOT / ".github" / "workflows" / "ci" / "bazel_dagger.py"


def _workflow(path: Path) -> dict[object, Any]:
    workflow = yaml.safe_load(path.read_text())
    assert isinstance(workflow, dict)
    return workflow


def _run_step(workflow: dict[object, Any]) -> dict[str, Any]:
    return next(
        step
        for step in workflow["jobs"]["unit_tests"]["steps"]
        if step.get("name") == "Run impacted Bazel unit tests in Dagger"
    )


def test_only_one_canonical_unit_test_job_runs_on_prs_and_main_pushes() -> None:
    workflow = _workflow(WORKFLOW)
    triggers = workflow.get("on") or workflow.get(True)
    assert set(triggers) == {"push", "pull_request", "workflow_call"}
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["pull_request"] == {"branches": ["main"]}
    assert set(workflow["jobs"]) == {"unit_tests"}
    assert workflow["jobs"]["unit_tests"]["name"] == "Unit tests"
    assert "if" not in workflow["jobs"]["unit_tests"]


def test_canonical_job_runs_impacted_bazel_in_one_dagger_call() -> None:
    workflow = _workflow(WORKFLOW)
    run_step = _run_step(workflow)
    assert (
        run_step["env"]["BAZEL_RUN_IMPACTED"]
        == "${{ inputs.run_impacted || github.event_name == 'push' || (github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository) }}"
    )
    assert (
        run_step["env"]["BAZEL_RUN_FULL"]
        == "${{ inputs.run_full || (github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository) }}"
    )
    assert "github.event.before" in run_step["env"]["BAZEL_DIFF_BASE_SHA"]
    assert run_step["env"]["JOB_NAME"] == "Unit tests"
    assert "TRUNK_API_TOKEN" in run_step["env"]
    assert "TRUNK_PR_NUMBER" in run_step["env"]
    assert "DAGGER_NO_NAG=1 dagger run python" in run_step["run"]

    pipeline = PIPELINE.read_text()
    assert "COMBINED_VALIDATE_CMD" in pipeline
    assert "BAZEL_RUN_FULL" in pipeline
    assert 'if [ "${{BAZEL_RUN_FULL:-false}}" = "true" ]' in pipeline
    assert "--build_event_json_file=/src/full_build_events.json" in pipeline
    assert "--build_event_json_file=/src/impacted_build_events.json" in pipeline
    assert "export HYPOTHESIS_PROFILE=ci" in pipeline


def test_impacted_targets_use_the_same_job_and_trunk_upload() -> None:
    workflow = _workflow(WORKFLOW)
    job = workflow["jobs"]["unit_tests"]
    assert job["runs-on"] == "ubuntu-24.04-arm"
    assert workflow["concurrency"] == {
        "group": "bazel-tests-${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }
    run_step = _run_step(workflow)
    assert "BAZEL_DAGGER_DIFF_JAR" in run_step["run"]
    assert "BAZEL_DAGGER_SOURCE_DIR" in run_step["run"]
    assert "bazel-diff_deploy.jar" in WORKFLOW.read_text()


def test_pipeline_computes_and_tests_impacted_targets_in_arm64() -> None:
    pipeline = PIPELINE.read_text()
    assert (
        'BASE_IMAGE = (\n    "ghcr.io/astral-sh/uv:0.11.29-python3.13-trixie-slim"\n'
        '    "@sha256:0b973c14a35cb0dc8fe63a2e8c9919fd797ac566de13090fcf0df4a6b3994b78"\n'
        ")"
    ) in pipeline
    assert 'dagger.Platform("linux/arm64")' in pipeline
    assert "TRUNK_BAZEL_ACTION_REV" in pipeline
    assert "build-essential" in pipeline
    assert (
        'BAZEL_DIFF_COMMAND_OPTIONS="--config=ci --incompatible_disallow_empty_glob=false"'
        in pipeline
    )
    assert "git checkout -q -f" in pipeline
    assert (
        'git rev-parse HEAD)" = "42994092b8b40711573f1111b9f34c742c9a371d"' in pipeline
    )
    assert (
        "BAZEL_STARTUP_OPTIONS=--output_user_root=/var/cache/bazel/output-user-root"
        in pipeline
    )
    assert 'source_dir = _required_env_path("BAZEL_DAGGER_SOURCE_DIR")' in pipeline
    assert 'mkdir --parents "${CACHE_DIR}"' in pipeline
    assert "prerequisites.sh" in pipeline
    assert "compute_impacted_targets.sh" in pipeline
    assert "test_impacted_targets.sh" in pipeline
    assert "--test_tag_filters=-manual" in pipeline
    assert "--nobuild_event_json_file_path_conversion" in pipeline
    assert "--build_event_json_file=/src/impacted_build_events.json" in pipeline
    assert '--bazel-bep-path="$bep"' in pipeline
    assert "--use-bazel-target-for-codeowners" in pipeline
    assert "--variant bazel" in pipeline
    assert "--allow-empty-test-results=false" in pipeline
    assert "--output /tmp/trunk-analytics-cli.tar.gz" in pipeline
    assert "sha256sum --check" in pipeline
    assert "tee /tmp/trunk-analytics-cli.tar.gz" not in pipeline
    assert pipeline.index("sha256sum --check") < pipeline.index(
        "tar --extract --gzip --file /tmp/trunk-analytics-cli.tar.gz",
    )
    assert "IMPACTED_TARGETS_PATH" in pipeline
    assert "CHANGED_PATHS_PATH" in pipeline
    assert "setImpactedTargets" in pipeline
    assert '"ALL"' in pipeline
    assert "--notest_keep_going" not in pipeline
    assert "--test_keep_going=false" not in pipeline
    assert 'export BAZEL_SCOPE_FILTER=""' in pipeline


def test_main_pushes_run_impacted_tests_without_pr_only_upload() -> None:
    pipeline = PIPELINE.read_text()
    assert "has_pr_metadata" in pipeline
    assert "BAZEL_RUN_IMPACTED must be 'true' or 'false'" in pipeline
    assert (
        'git update-ref refs/remotes/origin/main "${BAZEL_DIFF_BASE_SHA}"' in pipeline
    )
    assert "raise SystemExit(rc)" in pipeline


def test_full_workflow_is_scheduled_and_manual() -> None:
    workflow = _workflow(REPO_ROOT / ".github" / "workflows" / "tests-bazel-full.yml")
    triggers = workflow.get("on") or workflow.get(True)
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert workflow["jobs"]["full_suite"]["with"] == {
        "run_full": True,
        "run_impacted": False,
    }


def test_workflow_prepares_history_and_cache_before_dagger() -> None:
    workflow = _workflow(WORKFLOW)
    steps = workflow["jobs"]["unit_tests"]["steps"]
    names = [step.get("name") for step in steps]
    names.index("Prepare self-contained Git history")
    assert names.index("Cache Dagger controller and Bazel data") < names.index(
        "Prepare cached Dagger and Bazel paths",
    )
    assert names.index("Prepare cached Dagger and Bazel paths") < names.index(
        "Install Dagger Python SDK",
    )
    source_step = next(
        step
        for step in steps
        if step.get("name") == "Prepare self-contained Git history"
    )
    assert "https://github.com/${{ github.repository }}.git" in source_step["run"]
    cache = next(
        step
        for step in steps
        if step.get("name") == "Cache Dagger controller and Bazel data"
    )
    assert cache["uses"].startswith("actions/cache/restore@")
    assert "runner.arch" in cache["with"]["key"]
    for cache_path in (
        "~/.dagger-sdk/bazel-controller-venv",
        "~/.dagger-sdk/bazel-uv-python",
        "~/.bazel-dagger",
    ):
        assert cache_path in cache["with"]["path"]
