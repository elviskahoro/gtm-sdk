"""Contract tests for the Bazel pytest launcher."""

# ruff: noqa: INP001, S101, PLR2004 -- tests/bazel has no runtime package.

from __future__ import annotations

import configparser
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_PATH = REPO_ROOT / "bazel" / "pytest_main.py"
PYTEST_CONFIG_PATH = REPO_ROOT / "bazel" / "pytest.ini"
_MODULE_NAME = "_bazel_pytest_main_under_test"


class _FakePytest:
    def __init__(self, return_code: int) -> None:
        self.return_code = return_code
        self.calls: list[list[str]] = []
        self.cwd_at_call: Path | None = None

    def main(self, args: list[str]) -> int:
        self.calls.append(args)
        self.cwd_at_call = Path.cwd()
        return self.return_code


def _load_launcher_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, LAUNCHER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(_MODULE_NAME, None)
        sys.dont_write_bytecode = old_dont_write_bytecode
    return module


def _runfiles_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    test_srcdir = tmp_path / "runfiles"
    workspace = "gtm_sdk"
    root = test_srcdir / workspace
    root.mkdir(parents=True)
    monkeypatch.setenv("TEST_SRCDIR", str(test_srcdir))
    monkeypatch.setenv("TEST_WORKSPACE", workspace)
    monkeypatch.delenv("XML_OUTPUT_FILE", raising=False)
    return root


def test_workspace_root_uses_bazel_runfiles_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_launcher_module()
    root = _runfiles_root(tmp_path, monkeypatch)

    assert module.workspace_root() == root


def test_workspace_root_falls_back_to_repository_for_direct_uv_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_launcher_module()
    monkeypatch.delenv("TEST_SRCDIR", raising=False)
    monkeypatch.delenv("TEST_WORKSPACE", raising=False)

    assert module.workspace_root() == REPO_ROOT


def test_bazel_pytest_config_keeps_asyncio_strict_mode() -> None:
    config = configparser.ConfigParser()
    config.read(PYTEST_CONFIG_PATH, encoding="utf-8")

    assert config["pytest"]["asyncio_mode"] == "strict"
    assert "--ignore-glob=*/bazel-*" in config["pytest"]["addopts"]


def test_pytest_args_restore_project_configuration_and_bazel_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_launcher_module()
    root = _runfiles_root(tmp_path, monkeypatch)

    assert module.pytest_args(
        ["tests/bazel/test_pytest_main.py", "tests/libs/example/test_case.py"],
    ) == [
        "-c",
        str(root / "bazel" / "pytest.ini"),
        "--rootdir",
        str(root),
        "--import-mode=importlib",
        "-m",
        "not integration",
        str(root / "tests/bazel/test_pytest_main.py"),
        str(root / "tests/libs/example/test_case.py"),
    ]


def test_pytest_args_add_junit_xml_when_bazel_requests_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_launcher_module()
    root = _runfiles_root(tmp_path, monkeypatch)
    xml_output = tmp_path / "test.xml"
    monkeypatch.setenv("XML_OUTPUT_FILE", str(xml_output))

    assert module.pytest_args(["tests/bazel/test_pytest_main.py"]) == [
        "-c",
        str(root / "bazel" / "pytest.ini"),
        "--rootdir",
        str(root),
        "--import-mode=importlib",
        "-m",
        "not integration",
        f"--junitxml={xml_output}",
        str(root / "tests/bazel/test_pytest_main.py"),
    ]


def test_main_chdirs_to_workspace_before_invoking_pytest_and_returns_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_launcher_module()
    root = _runfiles_root(tmp_path, monkeypatch)
    other_cwd = tmp_path / "outside"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    monkeypatch.setattr(
        sys,
        "argv",
        ["pytest_main.py", "tests/bazel/test_pytest_main.py"],
    )
    fake_pytest = _FakePytest(return_code=17)
    monkeypatch.setattr(module, "pytest", fake_pytest)

    result = module.main()

    assert result == 17
    assert fake_pytest.cwd_at_call == root
    assert Path.cwd() == root
    assert len(fake_pytest.calls) == 1
    assert fake_pytest.calls[0] == [
        "-c",
        str(root / "bazel" / "pytest.ini"),
        "--rootdir",
        str(root),
        "--import-mode=importlib",
        "-m",
        "not integration",
        str(root / "tests/bazel/test_pytest_main.py"),
    ]
