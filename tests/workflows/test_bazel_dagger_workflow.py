# ruff: noqa: INP001, S101 -- workflow tests are standalone and assertion-based.
"""Static contracts for the parallel ARM64 Dagger impacted-target trial."""

from pathlib import Path
from typing import Any

import yaml

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests-bazel-dagger.yml"
STANDARD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests-bazel.yml"
PIPELINE = REPO_ROOT / ".github" / "workflows" / "ci" / "bazel_dagger.py"


def _workflow(path: Path) -> dict[object, Any]:
    workflow = yaml.safe_load(path.read_text())
    assert isinstance(workflow, dict)
    return workflow


def test_only_impacted_target_checks_run_for_pull_requests() -> None:
    dagger_workflow = _workflow(WORKFLOW)
    standard_workflow = _workflow(STANDARD_WORKFLOW)
    dagger_triggers = dagger_workflow.get("on") or dagger_workflow.get(True)
    standard_triggers = standard_workflow.get("on") or standard_workflow.get(True)
    assert dagger_triggers == {"pull_request": {"branches": ["main"]}}
    assert standard_triggers == {"pull_request": {"branches": ["main"]}}
    assert set(dagger_workflow["jobs"]) == {"bazel_impacted_dagger"}
    assert set(standard_workflow["jobs"]) == {"bazel_impacted"}


def test_impacted_target_trials_use_dagger_and_trunk() -> None:
    workflow = _workflow(WORKFLOW)
    impacted = workflow["jobs"]["bazel_impacted_dagger"]
    assert impacted["runs-on"] == "namespace-profile-test"
    assert (
        impacted["if"]
        == "github.event.pull_request.head.repo.full_name == github.repository"
    )
    assert workflow["concurrency"] == {
        "group": "arm64-dagger-bazel-${{ github.workflow }}-${{ github.ref }}",
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

    standard_workflow = STANDARD_WORKFLOW.read_text()
    assert "trunk-io/bazel-action@" in standard_workflow
    assert 'test-targets: "true"' in standard_workflow
    assert 'upload-targets: "true"' in standard_workflow


def test_pipeline_computes_and_tests_impacted_targets_in_arm64() -> None:
    pipeline = PIPELINE.read_text()
    assert 'dagger.Platform("linux/arm64")' in pipeline
    assert "TRUNK_BAZEL_ACTION_REV" in pipeline
    assert "BAZEL_DAGGER_DIFF_JAR" in pipeline
    assert 'source_dir = _required_env_path("BAZEL_DAGGER_SOURCE_DIR")' in pipeline
    assert 'mkdir --parents "${CACHE_DIR}"' in pipeline
    assert "prerequisites.sh" in pipeline
    assert "compute_impacted_targets.sh" in pipeline
    assert "test_impacted_targets.sh" in pipeline
    assert 'export BAZEL_SCOPE_FILTER=""' in pipeline
    assert "test //..." not in pipeline


def test_workflow_initializes_namespace_cache_mounts_before_dagger() -> None:
    workflow = _workflow(WORKFLOW)
    steps = workflow["jobs"]["bazel_impacted_dagger"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Prepare self-contained Git history") < names.index(
        "Run impacted Bazel targets in Dagger",
    )
    assert names.index("Prepare Namespace cache paths") < names.index(
        "Cache Dagger controller and Bazel data",
    )
    assert names.index("Cache Dagger controller and Bazel data") < names.index(
        "Initialize Namespace cache mounts",
    )
    assert names.index("Initialize Namespace cache mounts") < names.index(
        "Install Dagger Python SDK",
    )
    source_step = next(
        step
        for step in steps
        if step.get("name") == "Prepare self-contained Git history"
    )
    assert "https://github.com/${{ github.repository }}.git" in source_step["run"]
    for step_name in (
        "Prepare Namespace cache paths",
        "Initialize Namespace cache mounts",
    ):
        step = next(step for step in steps if step.get("name") == step_name)
        for cache_path in (
            "$HOME/.dagger-sdk/bazel-controller-venv",
            "$HOME/.dagger-sdk/bazel-uv-python",
            "$HOME/.bazel-dagger/toolchain",
            "$HOME/.bazel-dagger/cache",
        ):
            assert f'"{cache_path}"' in step["run"]
