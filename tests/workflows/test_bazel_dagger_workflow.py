# ruff: noqa: INP001, S101 -- workflow tests are standalone and assertion-based.
"""Static contracts for the parallel ARM64 Dagger/Bazel trial in #488."""

from pathlib import Path
from typing import Any

import yaml

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests-bazel-dagger.yml"
STANDARD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests-bazel.yml"
PIPELINE = REPO_ROOT / ".github" / "workflows" / "ci" / "bazel_dagger.py"


def _workflow() -> dict[object, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(workflow, dict)
    return workflow


def test_trial_runs_on_namespace_for_prs_and_main_pushes() -> None:
    workflow = _workflow()
    triggers = workflow.get("on") or workflow.get(True)
    assert isinstance(triggers, dict)
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    assert workflow["jobs"]["bazel_dagger"]["runs-on"] == "namespace-profile-test"
    assert workflow["concurrency"] == {
        "group": "arm64-dagger-bazel-${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }


def test_impacted_target_trial_runs_in_dagger_alongside_trunk() -> None:
    workflow = _workflow()
    impacted = workflow["jobs"]["bazel_impacted_dagger"]
    assert impacted["runs-on"] == "namespace-profile-test"
    assert "pull_request" in impacted["if"]
    assert "head.repo.full_name" in impacted["if"]
    run_step = next(
        step
        for step in impacted["steps"]
        if step.get("name") == "Run impacted Bazel targets in Dagger"
    )
    assert run_step["env"]["BAZEL_DAGGER_MODE"] == "impacted"
    assert "BAZEL_DAGGER_BASE_SHA" in run_step["env"]
    assert "BAZEL_DAGGER_HEAD_SHA" in run_step["env"]
    assert "BAZEL_DAGGER_DIFF_JAR" in run_step["run"]
    assert "bazel-diff_deploy.jar" in WORKFLOW.read_text()
    standard_workflow = STANDARD_WORKFLOW.read_text()
    assert "bazel_impacted:" in standard_workflow
    assert "trunk-io/bazel-action@" in standard_workflow


def test_trial_documents_its_equivalence_period() -> None:
    assert "three representative PR" in WORKFLOW.read_text()
    assert "one post-merge main run" in WORKFLOW.read_text()


def test_pipeline_uses_arm64_bazel_caches_and_exports_junit() -> None:
    pipeline = PIPELINE.read_text()
    assert 'dagger.Platform("linux/arm64")' in pipeline
    assert 'BAZEL_VERSION = "8.7.0"' in pipeline
    assert "BAZEL_DAGGER_BINARY" in pipeline
    assert "BAZEL_DAGGER_CACHE_DIR" in pipeline
    assert 'directory("/var/cache/bazel").export(str(cache_dir))' in pipeline
    assert "repository_cache=/var/cache/bazel/repository" in pipeline
    assert "disk_cache=/var/cache/bazel/disk" in pipeline
    assert "output_user_root=/var/cache/bazel/output-user-root" in pipeline
    assert 'test "$(bazel --version)" = "bazel {BAZEL_VERSION}"' in pipeline
    assert (
        "bazel --output_user_root=/var/cache/bazel/output-user-root test //... --config=ci"
        in pipeline
    )
    assert "GIT_INIT_CMD" in pipeline
    assert "git add --intent-to-add" in pipeline
    assert 'directory("/src/bazel-testlogs").export(JUNIT_HOST_PATH)' in pipeline
    assert "IMPACTED_VALIDATE_CMD" in pipeline
    assert 'mode == "impacted"' in pipeline
    assert 'directory("/src/.git", dag.host().directory(".git"))' in pipeline
    assert "get-impacted-targets" in pipeline
    assert "--target_pattern_file" in pipeline


def test_workflow_uploads_bazel_junit_results_to_trunk() -> None:
    workflow = WORKFLOW.read_text()
    assert "trunk-io/analytics-uploader@" in workflow
    assert "junit-paths: bazel-testlogs/**/test.xml" in workflow


def test_workflow_persists_the_controller_binary_and_bazel_caches() -> None:
    workflow = WORKFLOW.read_text()
    assert "nscloud-cache-action@" in workflow
    assert "~/.bazel-dagger" in workflow
    assert "bazel-controller-venv" in workflow
    assert "sha256sum --check" in workflow
    assert "bazel-8.7.0-linux-arm64" in workflow


def test_workflow_initializes_cold_namespace_cache_mounts() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["bazel_dagger"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Prepare Namespace cache paths") < names.index(
        "Cache Dagger controller and Bazel data",
    )
    assert names.index("Cache Dagger controller and Bazel data") < names.index(
        "Initialize Namespace cache mounts",
    )
    assert names.index("Initialize Namespace cache mounts") < names.index(
        "Install Dagger Python SDK",
    )
    cache_paths = (
        "$HOME/.dagger-sdk/bazel-controller-venv",
        "$HOME/.dagger-sdk/bazel-uv-python",
        "$HOME/.bazel-dagger/toolchain",
        "$HOME/.bazel-dagger/cache",
    )
    for step_name in (
        "Prepare Namespace cache paths",
        "Initialize Namespace cache mounts",
    ):
        step = next(step for step in steps if step.get("name") == step_name)
        for cache_path in cache_paths:
            assert f'"{cache_path}"' in step["run"]


def test_workflow_classifies_changes_before_starting_dagger() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["bazel_dagger"]["steps"]
    names = [step.get("name") for step in steps]
    classify_index = names.index("Classify changed paths")
    assert classify_index < names.index("Install uv")
    classifier = steps[classify_index]
    assert classifier["id"] == "classify"
    assert "scripts/bazel-change-classification.py" in classifier["run"]
    assert "git diff --name-status -z" in classifier["run"]
    assert "workflow_dispatch" in classifier["env"]["FORCE_FULL"]

    for step_name in (
        "Install uv",
        "Prepare Namespace cache paths",
        "Cache Dagger controller and Bazel data",
        "Initialize Namespace cache mounts",
        "Install Dagger Python SDK",
        "Cache verified ARM64 Bazel binary",
        "Setup Dagger",
        "Run full Bazel suite in Dagger",
    ):
        step = next(step for step in steps if step.get("name") == step_name)
        assert step["if"] == "steps.classify.outputs.run_full == 'true'"

    skipped = next(
        step for step in steps if step.get("name") == "Report skipped full suite"
    )
    assert skipped["if"] == "steps.classify.outputs.run_full == 'false'"
