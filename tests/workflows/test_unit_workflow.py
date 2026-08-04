# ruff: noqa: S101 -- asserts are the point of a workflow contract test.

"""Static contracts for the GitHub-hosted ARM64 unit-test workflow."""

import json
import tomllib
from pathlib import Path

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "tests-unit.yml"
PYTEST_DAGGER = (
    Path(__file__).parents[2] / ".github" / "workflows" / "ci" / "pytest_dagger.py"
)
PYTEST_DEPENDENCY_PACKER = (
    Path(__file__).parents[2]
    / ".github"
    / "workflows"
    / "ci"
    / "pytest_dependency_pack.py"
)
FLOX_MANIFEST = Path(__file__).parents[2] / ".flox" / "env" / "manifest.toml"
FLOX_MANIFEST_LOCK = FLOX_MANIFEST.with_name("manifest.lock")
FLOX_TOOLCHAIN_MANIFEST = (
    Path(__file__).parents[2] / "flox" / "toolchain" / ".flox" / "env" / "manifest.toml"
)
FLOX_TOOLCHAIN_LOCK = FLOX_TOOLCHAIN_MANIFEST.with_name("manifest.lock")
PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"
UV_LOCK = Path(__file__).parents[2] / "uv.lock"
PYTEST_INTEGRATION_DAGGER = (
    Path(__file__).parents[2]
    / ".github"
    / "workflows"
    / "ci"
    / "pytest_integration_dagger.py"
)

CHECKOUT = "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd"
CACHE = "actions/cache/"


def test_unit_workflow_uses_github_hosted_arm64_and_github_cache() -> None:
    workflow = WORKFLOW.read_text()

    assert "runs-on: ubuntu-24.04-arm" in workflow
    assert CHECKOUT in workflow
    assert "fetch-depth: 0" in workflow
    assert CACHE in workflow
    assert "actions/cache/restore@" in workflow
    assert "actions/cache/save@" in workflow
    assert "path: ~/.dagger-sdk" in workflow
    assert "runner.os }}-${{ runner.arch" in workflow
    assert "hashFiles('uv.lock', '.github/workflows/tests-unit.yml')" in workflow
    assert "enable-cache: true" in workflow


def test_unit_workflow_has_no_namespace_dependency() -> None:
    workflow = WORKFLOW.read_text()
    dagger = PYTEST_DAGGER.read_text()

    for source in (workflow, dagger):
        assert "namespacelabs/" not in source
        assert "namespace-profile" not in source
        assert "NSC_" not in source
        assert "NAMESPACE_" not in source
    assert not (WORKFLOW.parent.parent / "actionlint.yaml").exists()
    assert not (PYTEST_DEPENDENCY_PACKER.parent / "pytest_dependency_key.py").exists()


def test_unit_workflow_keeps_trusted_pr_controller() -> None:
    workflow = WORKFLOW.read_text()
    run_step = workflow.split("- name: Run pytest in Dagger", 1)[1]

    assert 'git show "${BASE_SHA}:.github/workflows/ci/pytest_dagger.py"' in run_step
    assert (
        'git show "${BASE_SHA}:.github/workflows/ci/pytest_dependency_pack.py"'
        in run_step
    )
    assert "PYTEST_DEPENDENCY_PACKER" in run_step
    assert "pytest-deps.Dockerfile" not in run_step
    assert "PYTEST_DEPENDENCY_DOCKERFILE" not in run_step
    assert "PYTEST_DEPENDENCY_IMAGE" not in workflow


def test_unit_dagger_builds_locked_arm64_dependencies_locally() -> None:
    dagger = PYTEST_DAGGER.read_text()

    assert 'dagger.Platform("linux/arm64")' in dagger
    assert "flox_toolchain_image(source)" in dagger
    assert "Pytest dependency image: building from the Flox manifest" in dagger
    assert "PYTEST_DEPENDENCY_IMAGE" not in dagger
    assert ".with_registry_auth" not in dagger
    assert "FLOX_MANIFEST_PATH" in dagger
    assert ".with_file(DEPENDENCY_PACKER_PATH, packer)" in dagger


def test_unit_workflow_keeps_resilient_local_dagger_execution() -> None:
    workflow = WORKFLOW.read_text()

    assert "version: 0.21.7" in workflow
    assert "timeout 150 docker pull" in workflow
    assert "for attempt in $(seq 1 6)" in workflow
    assert 'DAGGER_NO_NAG=1 dagger run python "${pipeline}"' in workflow
    assert "dagger --cloud" not in workflow
    assert "trunk-io/analytics-uploader@" in workflow


def test_local_dagger_commands_provision_the_project_environment() -> None:
    for path in (PYTEST_DAGGER, PYTEST_INTEGRATION_DAGGER):
        source = path.read_text()
        assert "uv run dagger run python" in source
        assert "dagger run .venv/bin/python" not in source


def test_unit_dependency_image_contains_only_locked_dependencies() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text())
    lock = tomllib.loads(UV_LOCK.read_text())
    manifest = tomllib.loads(FLOX_MANIFEST.read_text())
    manifest_lock = json.loads(FLOX_MANIFEST_LOCK.read_text())
    toolchain_manifest = tomllib.loads(FLOX_TOOLCHAIN_MANIFEST.read_text())
    toolchain_lock = json.loads(FLOX_TOOLCHAIN_LOCK.read_text())

    assert PYTEST_DEPENDENCY_PACKER.is_file()
    assert "unit-ci" in pyproject["dependency-groups"]
    assert any(package["name"] == "setuptools" for package in lock["package"])
    assert manifest["install"]["python313"]["pkg-path"] == "python313"
    assert manifest["install"]["uv"]["version"] == "0.11.26"
    assert manifest_lock["manifest"]["install"]["python313"]["pkg-path"] == "python313"
    assert toolchain_manifest["install"]["libgcc"]["pkg-path"] == "libgcc"
    assert toolchain_manifest["install"]["libgcc"]["systems"] == [
        "aarch64-linux",
        "x86_64-linux",
    ]
    assert toolchain_lock["manifest"]["install"]["libgcc"]["pkg-path"] == "libgcc"


def test_unit_pipeline_checks_binary_runtime_dependencies() -> None:
    dagger = PYTEST_DAGGER.read_text()

    assert "RUNTIME_CHECK_CMD" in dagger
    assert "/opt/venv/bin/python -c 'import duckdb'" in dagger
    assert "missing libstdc++.so.6" in dagger
    assert "runtime_checked = installed.with_exec" in dagger


def test_unit_workflow_supports_checkpoint_benchmarks() -> None:
    workflow = WORKFLOW.read_text()

    for variant in (
        "full-compiled",
        "full-source",
        "minimal-compiled",
        "minimal-expanded",
        "minimal-packed",
    ):
        assert f"- {variant}" in workflow
    assert "benchmark_nonce:" in workflow
    assert "PYTEST_BENCHMARK_NONCE" in workflow
