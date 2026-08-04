"""Static contracts for the parallel ARM64 Dagger/Bazel trial in #488."""

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests-bazel-dagger.yml"
PIPELINE = REPO_ROOT / ".github" / "workflows" / "ci" / "bazel_dagger.py"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def test_trial_runs_on_namespace_for_prs_and_main_pushes() -> None:
    workflow = _workflow()
    triggers = workflow.get("on") or workflow[True]
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    assert workflow["jobs"]["bazel_dagger"]["runs-on"] == "namespace-profile-test"


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
    assert "${BAZEL} test //... --config=ci" in pipeline
    assert "GIT_INIT_CMD" in pipeline
    assert "git add --intent-to-add" in pipeline
    assert 'directory("/src/bazel-testlogs").export(JUNIT_HOST_PATH)' in pipeline


def test_workflow_uploads_bazel_junit_results_to_trunk() -> None:
    workflow = WORKFLOW.read_text()
    assert "trunk-io/analytics-uploader@" in workflow
    assert 'junit-paths: "bazel-testlogs/**/test.xml"' in workflow


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
    assert names.index("Cache Dagger controller and Bazel data") < names.index(
        "Initialize Namespace cache mounts",
    )
    assert names.index("Initialize Namespace cache mounts") < names.index(
        "Install Dagger Python SDK",
    )
    initialize = next(
        step for step in steps if step.get("name") == "Initialize Namespace cache mounts"
    )
    for cache_path in (
        "$HOME/.dagger-sdk/bazel-controller-venv",
        "$HOME/.dagger-sdk/bazel-uv-python",
        "$HOME/.bazel-dagger/toolchain",
        "$HOME/.bazel-dagger/cache",
    ):
        assert f'"{cache_path}"' in initialize["run"]
