# ruff: noqa: S101 -- asserts are the point of a workflow contract test.

"""Static contracts for the GitHub-hosted ARM64 unit-test workflow."""

import tomllib
from pathlib import Path

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "tests-unit.yml"
PYTEST_DAGGER = (
    Path(__file__).parents[2] / ".github" / "workflows" / "ci" / "pytest_dagger.py"
)
PYTEST_DEPENDENCY_DOCKERFILE = (
    Path(__file__).parents[2]
    / ".github"
    / "workflows"
    / "ci"
    / "pytest-deps.Dockerfile"
)
PYTEST_DEPENDENCY_DOCKERIGNORE = PYTEST_DEPENDENCY_DOCKERFILE.with_name(
    f"{PYTEST_DEPENDENCY_DOCKERFILE.name}.dockerignore",
)
PYTEST_DEPENDENCY_PACKER = PYTEST_DEPENDENCY_DOCKERFILE.with_name(
    "pytest_dependency_pack.py",
)
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
    assert not PYTEST_DEPENDENCY_DOCKERFILE.with_name(
        "pytest_dependency_key.py",
    ).exists()


def test_unit_workflow_keeps_trusted_pr_controller() -> None:
    workflow = WORKFLOW.read_text()
    run_step = workflow.split("- name: Run pytest in Dagger", 1)[1]

    assert 'git show "${BASE_SHA}:.github/workflows/ci/pytest_dagger.py"' in run_step
    assert (
        'git show "${BASE_SHA}:.github/workflows/ci/pytest-deps.Dockerfile"' in run_step
    )
    assert (
        'git show "${BASE_SHA}:.github/workflows/ci/pytest_dependency_pack.py"'
        in run_step
    )
    assert "PYTEST_DEPENDENCY_DOCKERFILE" in run_step
    assert "PYTEST_DEPENDENCY_PACKER" in run_step
    assert "PYTEST_DEPENDENCY_IMAGE" not in workflow


def test_unit_dagger_builds_locked_arm64_dependencies_locally() -> None:
    dagger = PYTEST_DAGGER.read_text()

    assert 'dagger.Platform("linux/arm64")' in dagger
    assert ".docker_build(" in dagger
    assert "Pytest dependency image: building from the locked Dockerfile" in dagger
    assert "PYTEST_DEPENDENCY_IMAGE" not in dagger
    assert ".with_registry_auth" not in dagger
    assert "pytest-deps.Dockerfile" in dagger
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
    dockerfile = PYTEST_DEPENDENCY_DOCKERFILE.read_text()
    dockerignore = PYTEST_DEPENDENCY_DOCKERIGNORE.read_text()
    pyproject = tomllib.loads(PYPROJECT.read_text())
    lock = tomllib.loads(UV_LOCK.read_text())

    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "COPY . " not in dockerfile
    assert "--locked" in dockerfile
    assert "pytest_dependency_pack.py" in dockerfile
    assert PYTEST_DEPENDENCY_PACKER.is_file()
    assert dockerignore.splitlines()[-1] == (
        "!.github/workflows/ci/pytest_dependency_pack.py"
    )
    assert "unit-ci" in pyproject["dependency-groups"]
    assert any(package["name"] == "setuptools" for package in lock["package"])


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
