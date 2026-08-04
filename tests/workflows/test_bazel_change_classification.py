# ruff: noqa: ANN201, INP001, PT006, PT018, S101
"""Tests for the conservative ARM64 Dagger Bazel CI diff classifier."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "bazel-change-classification.py"
SPEC = importlib.util.spec_from_file_location("bazel_change_classification", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def changes(*pairs: tuple[str, str]):
    return [MODULE.Change(status=status, path=path) for status, path in pairs]


@pytest.mark.parametrize(
    ("path",),
    [
        ("README.md",),
        ("CHANGELOG.md",),
        ("docs/telemetry/overview.mdx",),
        (".github/ISSUE_TEMPLATE/bug.yml",),
        (".git-blame-ignore-revs",),
    ],
)
def test_approved_metadata_only_changes_can_skip(path: str) -> None:
    run_full, _ = MODULE.classify(changes(("M", path)))
    assert not run_full


@pytest.mark.parametrize(
    ("path",),
    [
        ("src/attio/people.py",),
        ("tests/libs/attio/test_people.py",),
        ("pyproject.toml",),
        ("uv.lock",),
        ("BUILD.bazel",),
        (".github/workflows/tests-bazel.yml",),
        ("scripts/example.py",),
        ("unknown.txt",),
    ],
)
def test_unknown_and_runtime_changes_require_full_suite(path: str) -> None:
    run_full, _ = MODULE.classify(changes(("M", path)))
    assert run_full


@pytest.mark.parametrize("path", ["docs/BUILD.bazel", "docs/custom.bzl"])
def test_bazel_control_files_under_docs_require_full_suite(path: str) -> None:
    run_full, _ = MODULE.classify(changes(("M", path)))
    assert run_full


@pytest.mark.parametrize("status", ["D", "R100", "C100", "T"])
def test_deletes_renames_copies_and_mode_changes_require_full_suite(
    status: str,
) -> None:
    run_full, _ = MODULE.classify(changes((status, "README.md")))
    assert run_full


def test_mixed_changes_require_full_suite() -> None:
    run_full, _ = MODULE.classify(
        changes(("M", "docs/README.md"), ("M", "libs/attio/client.py")),
    )
    assert run_full


def test_force_full_overrides_irrelevant_diff() -> None:
    run_full, reason = MODULE.classify(
        changes(("M", "README.md")),
        force_full=True,
    )
    assert run_full
    assert "force-full" in reason


def test_parse_nul_delimited_changes_including_rename() -> None:
    raw = b"M\0README.md\0R100\0README.md\0docs/README.md\0"
    assert MODULE.parse_changes(raw) == changes(
        ("M", "README.md"),
        ("R100", "README.md"),
        ("R100", "docs/README.md"),
    )
