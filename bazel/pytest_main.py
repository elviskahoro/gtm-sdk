"""Bazel-native pytest entrypoint shared by generated test targets."""

# ruff: noqa: INP001 -- bazel/ is a Bazel package, not a setuptools package.

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def workspace_root() -> Path:
    """Resolve the repository root under Bazel runfiles or direct execution."""
    test_srcdir = os.environ.get("TEST_SRCDIR")
    test_workspace = os.environ.get("TEST_WORKSPACE")
    if test_srcdir and test_workspace:
        return Path(test_srcdir) / test_workspace
    return Path(__file__).resolve().parents[1]


def pytest_args(test_paths: list[str]) -> list[str]:
    root = workspace_root()
    args = [
        "-c",
        str(root / "bazel" / "pytest.ini"),
        "--import-mode=importlib",
        "-m",
        "not integration",
    ]
    if xml := os.environ.get("XML_OUTPUT_FILE"):
        args.append(f"--junitxml={xml}")
    return [*args, *(str(root / path) for path in test_paths)]


def main() -> int:
    root = workspace_root()
    os.chdir(root)
    return int(pytest.main(pytest_args(sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover - exercised through Bazel.
    raise SystemExit(main())
