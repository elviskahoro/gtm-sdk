"""Contracts for the immutable Flox CI image publication workflow."""

# ruff: noqa: INP001 -- workflow contract test loaded by pytest.

# ruff: noqa: S101

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "flox-image-publish.yml"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_publishes_both_architectures_from_hosted_runners() -> None:
    matrix = _workflow()["jobs"]["publish"]["strategy"]["matrix"]["include"]
    assert {entry["architecture"] for entry in matrix} == {"arm64", "amd64"}
    assert {entry["runner"] for entry in matrix} == {
        "ubuntu-24.04-arm",
        "ubuntu-24.04",
    }


def test_publish_tags_are_commit_and_architecture_specific() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "flox-toolchain:${{ github.sha }}-${{ matrix.architecture }}" in source
    assert "pytest-deps:${{ github.sha }}-${{ matrix.architecture }}" in source
    assert "pytest_dependency_image_publish.py" in source
    assert "steps.publish_toolchain.outputs.image_ref" in source


def test_latest_is_only_requested_for_default_branch() -> None:
    workflow = _workflow()
    assert workflow["permissions"]["packages"] == "write"
    source = (ROOT / ".github" / "workflows" / "ci" / "flox_image_dagger.py").read_text(
        encoding="utf-8",
    )
    assert 'FLOX_PUBLISH_LATEST") == "true"' in source
    publish_workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'ARCHITECTURE}" == "arm64"' in publish_workflow


def test_pytest_image_publish_checks_locked_uv_and_runtime_tools() -> None:
    source = (
        ROOT / ".github" / "workflows" / "ci" / "pytest_dependency_image_publish.py"
    ).read_text(encoding="utf-8")
    assert "manifest.lock" in source
    assert "expected_uv_version" in source
    assert "runtime_provenance_command" in source
    assert "GITHUB_OUTPUT" in source
