"""Bazel-native pytest entrypoint shared by every generated ``pytest_test``.

PR2 Task 9 (plan ai-m4p9). Each Gazelle-generated ``py_test`` target is mapped
to the ``pytest_test`` macro (``bazel/pytest_test.bzl``), which pins
``main = //bazel:pytest_main.py`` so every target runs through this module.
It resolves the gtm-sdk workspace root, then invokes pytest with the
repository's authoritative configuration from ``pyproject.toml`` -- so a
Bazel test run selects the same files, markers, and import mode as
``uv run pytest`` for the given paths, and reports JUnit XML to the path
Bazel already expects (``$XML_OUTPUT_FILE``) without inventing its own
discovery or reporting behavior.

Direct invocation (``uv run python bazel/pytest_main.py <paths>``) is the
path the unit tests in ``tests/bazel/test_pytest_main.py`` exercise; it
exercises the same code as the Bazel run, just without runfiles env vars.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def workspace_root() -> Path:
    """Resolve the gtm-sdk workspace root.

    Under Bazel, ``$TEST_SRCDIR/$TEST_WORKSPACE`` is the runfiles root
    (``<runfiles>/<workspace>``); ``pyproject.toml`` and the test tree hang
    off it, and the macro's ``data`` attr ensures ``pyproject.toml`` lands in
    runfiles. When the launcher is run directly (``uv run python
    bazel/pytest_main.py ...``) -- the path the unit tests exercise -- there
    are no runfiles env vars, so fall back to the repo root two levels up
    from this file (``bazel/pytest_main.py`` -> ``bazel/`` -> repo root).
    """
    srcdir = os.environ.get("TEST_SRCDIR")
    workspace = os.environ.get("TEST_WORKSPACE")
    if srcdir and workspace:
        return Path(srcdir) / workspace
    return Path(__file__).resolve().parents[1]


def pytest_args(test_paths: list[str]) -> list[str]:
    """Build the pytest argv matching ``pyproject.toml``'s ``addopts``.

    ``-c <root>/pyproject.toml`` makes pytest read the authoritative config
    (markers, filterwarnings, import mode) regardless of the launch cwd;
    ``--import-mode=importlib`` and ``-m 'not integration'`` mirror
    ``[tool.pytest.ini_options].addopts`` so Bazel and uv select the same
    tests; ``--junitxml`` is emitted only when Bazel sets
    ``$XML_OUTPUT_FILE`` so the runner never invents its own report path.
    The collected test paths (passed through from the target's ``args``)
    come last, exactly as ``uv run pytest <paths>`` would receive them.
    """
    root = workspace_root()
    args: list[str] = [
        "-c",
        str(root / "pyproject.toml"),
        "--import-mode=importlib",
        "-m",
        "not integration",
    ]
    xml_output = os.environ.get("XML_OUTPUT_FILE")
    if xml_output:
        args.extend(["--junitxml", xml_output])
    args.extend(test_paths)
    return args


def main(argv: list[str] | None = None) -> int:
    """Entrypoint: chdir to the workspace root and run pytest.

    ``chdir`` is essential: importlib mode + ``-c <root>/pyproject.toml``
    resolve test paths and the config relative to the workspace root, so the
    process cwd must be that root regardless of where Bazel launched it.
    """
    os.chdir(workspace_root())
    raw_args = argv if argv is not None else sys.argv[1:]
    return int(pytest.main(pytest_args(list(raw_args))))


if __name__ == "__main__":  # pragma: no cover - exercised by ``bazel test``.
    raise SystemExit(main())
