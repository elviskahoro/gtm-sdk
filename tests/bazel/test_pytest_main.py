"""Unit tests for the Bazel-native pytest launcher (``bazel/pytest_main.py``).

PR2 Task 9 (plan ai-m4p9). These cover the launcher's two resolution modes
(Bazel runfiles vs. direct-uv fallback) and the pytest argv contract so a
``bazel test`` run is provably equivalent to ``uv run pytest`` for the same
paths: authoritative ``-c`` config, importlib mode, the ``not integration``
marker, JUnit XML only when Bazel sets ``$XML_OUTPUT_FILE``, and the pytest
exit code passed through as the process exit code. ``pytest.main`` itself is
stubbed so no tests are actually collected or run here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import bazel.pytest_main as pm


class _LauncherRecorder:
    """Captures what the launcher passed to its stubbed ``os.chdir``/``pytest.main``."""

    def __init__(self, return_code: int = 0) -> None:
        self.chdir_calls: list[Path] = []
        self.pytest_argv: list[str] | None = None
        self.return_code = return_code

    def chdir(self, path: str | Path) -> None:
        self.chdir_calls.append(Path(path))

    def run_pytest(self, args: list[str]) -> int:
        self.pytest_argv = args
        return self.return_code


def test_workspace_root_uses_runfiles_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under Bazel, the root is ``$TEST_SRCDIR/$TEST_WORKSPACE``."""
    monkeypatch.setenv("TEST_SRCDIR", "/srv/bazel/xyz/_main/_runfiles")
    monkeypatch.setenv("TEST_WORKSPACE", "gtm")
    assert pm.workspace_root() == Path("/srv/bazel/xyz/_main/_runfiles/gtm")


def test_workspace_root_falls_back_to_file_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct ``uv`` invocation has no runfiles env; root is two dirs up."""
    monkeypatch.delenv("TEST_SRCDIR", raising=False)
    monkeypatch.delenv("TEST_WORKSPACE", raising=False)
    expected = Path(pm.__file__).resolve().parents[1]
    assert pm.workspace_root() == expected


def test_pytest_args_includes_authoritative_config_and_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``$XML_OUTPUT_FILE``, args mirror ``addopts`` + test paths."""
    monkeypatch.delenv("XML_OUTPUT_FILE", raising=False)
    root = pm.workspace_root()
    args = pm.pytest_args(["tests/libs/attio/test_models.py"])
    assert args == [
        "-c",
        str(root / "pyproject.toml"),
        "--import-mode=importlib",
        "-m",
        "not integration",
        "tests/libs/attio/test_models.py",
    ]
    assert "--junitxml" not in args


def test_pytest_args_emits_junitxml_when_xml_output_file_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bazel sets ``$XML_OUTPUT_FILE``; the launcher reports JUnit there."""
    monkeypatch.setenv("XML_OUTPUT_FILE", "/srv/bazel/_outputs/test.xml")
    root = pm.workspace_root()
    args = pm.pytest_args(["tests/libs/dlt/test_x.py"])
    prefix = [
        "-c",
        str(root / "pyproject.toml"),
        "--import-mode=importlib",
        "-m",
        "not integration",
        "--junitxml",
        "/srv/bazel/_outputs/test.xml",
    ]
    assert args[: len(prefix)] == prefix
    assert args[len(prefix) :] == ["tests/libs/dlt/test_x.py"]


def test_main_chdirs_to_workspace_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` cwd's the process to the resolved workspace root."""
    rec = _LauncherRecorder()
    monkeypatch.setattr(pm.os, "chdir", rec.chdir)
    monkeypatch.setattr(pm.pytest, "main", rec.run_pytest)
    pm.main([])
    assert rec.chdir_calls == [pm.workspace_root()]


def test_main_returns_pytest_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pytest exit code is the process exit code (passed through as int)."""
    for raw in (0, 1, 2, 5):
        rec = _LauncherRecorder(return_code=raw)
        monkeypatch.setattr(pm.os, "chdir", rec.chdir)
        monkeypatch.setattr(pm.pytest, "main", rec.run_pytest)
        assert pm.main([]) == raw


def test_main_forwards_built_args_to_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` passes ``pytest_args(argv)`` to ``pytest.main`` verbatim."""
    rec = _LauncherRecorder()
    monkeypatch.setattr(pm.os, "chdir", rec.chdir)
    monkeypatch.setattr(pm.pytest, "main", rec.run_pytest)
    pm.main(["tests/a/test_b.py"])
    root = pm.workspace_root()
    assert rec.pytest_argv == [
        "-c",
        str(root / "pyproject.toml"),
        "--import-mode=importlib",
        "-m",
        "not integration",
        "tests/a/test_b.py",
    ]
