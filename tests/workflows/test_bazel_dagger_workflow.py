# ruff: noqa: INP001, S101, S105 -- workflow tests are standalone and assertion-based.
"""Static contracts for the ARM64 Dagger impacted-target Bazel workflow."""

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


def test_only_impacted_target_check_runs_for_pull_requests() -> None:
    workflow = _workflow(WORKFLOW)
    triggers = workflow.get("on") or workflow.get(True)
    assert triggers == {"pull_request": {"branches": ["main"]}}
    assert set(workflow["jobs"]) == {"bazel_impacted"}


def test_impacted_targets_use_dagger_and_trunk() -> None:
    workflow = _workflow(WORKFLOW)
    impacted = workflow["jobs"]["bazel_impacted"]
    assert impacted["runs-on"] == "ubuntu-24.04-arm"
    assert (
        impacted["if"]
        == "github.event.pull_request.head.repo.full_name == github.repository"
    )
    assert workflow["concurrency"] == {
        "group": "bazel-tests-${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }
    run_step = next(
        step
        for step in impacted["steps"]
        if step.get("name") == "Run impacted Bazel targets in Dagger"
    )
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
        "BAZEL_STARTUP_OPTIONS=--output_user_root=/var/cache/bazel/output-user-root"
        in pipeline
    )
    assert "BAZEL_DAGGER_DIFF_JAR" in pipeline
    assert 'source_dir = _required_env_path("BAZEL_DAGGER_SOURCE_DIR")' in pipeline
    assert 'mkdir --parents "${CACHE_DIR}"' in pipeline
    assert "prerequisites.sh" in pipeline
    assert "compute_impacted_targets.sh" in pipeline
    assert "test_impacted_targets.sh" in pipeline
    assert "--test_tag_filters=-manual" in pipeline
    assert "--nobuild_event_json_file_path_conversion" in pipeline
    assert "--build_event_json_file=/src/build_events.json" in pipeline
    assert "--bazel-bep-path={BEP_PATH}" in pipeline
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
    assert "test //..." not in pipeline


def test_workflow_restores_github_cache_before_dagger() -> None:
    workflow = _workflow(WORKFLOW)
    steps = workflow["jobs"]["bazel_impacted"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Prepare self-contained Git history") < names.index(
        "Run impacted Bazel targets in Dagger",
    )
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


def test_workflow_passes_same_repo_pr_metadata_and_token_to_dagger() -> None:
    workflow = _workflow(WORKFLOW)
    steps = workflow["jobs"]["bazel_impacted"]["steps"]
    run_step = next(
        step
        for step in steps
        if step.get("name") == "Run impacted Bazel targets in Dagger"
    )
    env = run_step["env"]
    assert env["TRUNK_API_TOKEN"] == "${{ secrets.TRUNK_API_TOKEN }}"
    assert env["TRUNK_REPOSITORY"] == "${{ github.repository }}"
    assert env["TRUNK_PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    assert env["TRUNK_PR_HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert env["TRUNK_PR_BASE_REF"] == "${{ github.event.pull_request.base.ref }}"
    assert env["CUSTOM"] == "true"
