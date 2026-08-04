"""Contract tests for scripts/bazel-requirements-sync.py."""

# ruff: noqa: S101, PLR2004 -- asserts and literal exit codes keep these tests direct.

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_SCRIPT_PATH = REPO_ROOT / "scripts" / "bazel-requirements-sync.py"
_MODULE_NAME = "_bazel_requirements_sync_under_test"

EXPECTED_EXPORT_COMMAND = [
    "uv",
    "export",
    "--frozen",
    "--format",
    "requirements.txt",
    "--no-default-groups",
    "--group",
    "dev",
    "--no-emit-workspace",
    "--no-header",
    "--no-annotate",
]


class _FakeUvExport:
    def __init__(
        self,
        *,
        stdout: str,
        returncode: int = 0,
        stderr: str = "",
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self,
        cmd: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        command = cast("list[str]", cmd)
        self.calls.append((command, dict(kwargs)))
        if kwargs.get("check") is True and self.returncode != 0:
            raise subprocess.CalledProcessError(
                self.returncode,
                command,
                output=self.stdout,
                stderr=self.stderr,
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def _load_script_module(script_path: Path) -> ModuleType:
    """Import the hyphenated script by path, as the sibling script tests do."""
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, script_path)
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


def _load_temp_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ModuleType, Path, Path]:
    assert SOURCE_SCRIPT_PATH.exists(), f"{SOURCE_SCRIPT_PATH} must exist"

    tmp_repo = tmp_path / "repo"
    script_path = tmp_repo / "scripts" / SOURCE_SCRIPT_PATH.name
    script_path.parent.mkdir(parents=True)
    script_path.write_text(SOURCE_SCRIPT_PATH.read_text(encoding="utf-8"))

    other_cwd = tmp_path / "cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    return _load_script_module(script_path), tmp_repo, script_path


def _patch_uv_export(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str,
    returncode: int = 0,
    stderr: str = "",
) -> _FakeUvExport:
    fake = _FakeUvExport(stdout=stdout, returncode=returncode, stderr=stderr)

    patched = False
    if hasattr(module, "subprocess"):
        monkeypatch.setattr(module.subprocess, "run", fake)
        patched = True
    if hasattr(module, "run"):
        monkeypatch.setattr(module, "run", fake)
        patched = True

    assert patched, "script must invoke uv through subprocess.run"
    return fake


def _run_main(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    script_path: Path,
    *args: str,
) -> int:
    monkeypatch.setattr(sys, "argv", [str(script_path), *args])
    try:
        result = module.main()
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if not isinstance(exc.code, int):
            msg = f"main() exited with a non-integer code: {exc.code!r}"
            raise TypeError(msg) from exc
        return exc.code

    assert isinstance(result, int)
    return result


def _assert_uv_export_call(
    call: tuple[list[str], dict[str, object]],
    *,
    cwd: Path,
) -> None:
    cmd, kwargs = call
    assert isinstance(cmd, list)
    assert cmd == EXPECTED_EXPORT_COMMAND
    cwd_arg = kwargs["cwd"]
    assert isinstance(cwd_arg, Path)
    assert cwd_arg.resolve() == cwd


def _generated_files(tmp_repo: Path, script_path: Path) -> list[Path]:
    return sorted(
        path
        for path in tmp_repo.rglob("*")
        if path.is_file() and path != script_path and "__pycache__" not in path.parts
    )


def _only_generated_file(tmp_repo: Path, script_path: Path) -> Path:
    generated = _generated_files(tmp_repo, script_path)
    assert [path.relative_to(tmp_repo) for path in generated]
    assert len(generated) == 1
    return generated[0]


def test_default_export_command_runs_from_script_repo_root_and_writes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, tmp_repo, script_path = _load_temp_script(tmp_path, monkeypatch)
    fake_uv = _patch_uv_export(
        module,
        monkeypatch,
        stdout="alpha==1.0\nbeta==2.0",
    )

    exit_code = _run_main(module, monkeypatch, script_path)

    assert exit_code == 0
    assert len(fake_uv.calls) == 1
    _assert_uv_export_call(fake_uv.calls[0], cwd=tmp_repo)
    assert _only_generated_file(tmp_repo, script_path).read_text(encoding="utf-8") == (
        "alpha==1.0\nbeta==2.0\n"
    )


def test_check_identical_returns_zero_without_rewriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, tmp_repo, script_path = _load_temp_script(tmp_path, monkeypatch)
    fake_uv = _patch_uv_export(module, monkeypatch, stdout="alpha==1.0")
    assert _run_main(module, monkeypatch, script_path) == 0
    requirements_path = _only_generated_file(tmp_repo, script_path)

    sentinel_ns = 1_700_000_000_123_456_789
    os.utime(requirements_path, ns=(sentinel_ns, sentinel_ns))
    before_mtime_ns = requirements_path.stat().st_mtime_ns

    assert _run_main(module, monkeypatch, script_path, "--check") == 0

    assert len(fake_uv.calls) == 2
    _assert_uv_export_call(fake_uv.calls[1], cwd=tmp_repo)
    assert requirements_path.stat().st_mtime_ns == before_mtime_ns
    assert requirements_path.read_text(encoding="utf-8") == "alpha==1.0\n"


def test_check_missing_file_returns_one_and_prints_unified_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, tmp_repo, script_path = _load_temp_script(tmp_path, monkeypatch)
    fake_uv = _patch_uv_export(module, monkeypatch, stdout="alpha==1.0")

    exit_code = _run_main(module, monkeypatch, script_path, "--check")

    captured = capsys.readouterr()
    diff = captured.out + captured.err
    assert exit_code == 1
    assert len(fake_uv.calls) == 1
    _assert_uv_export_call(fake_uv.calls[0], cwd=tmp_repo)
    assert "--- " in diff
    assert "+++ " in diff
    assert "@@ " in diff
    assert "+alpha==1.0" in diff
    assert _generated_files(tmp_repo, script_path) == []


def test_check_stale_file_returns_one_and_prints_unified_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, tmp_repo, script_path = _load_temp_script(tmp_path, monkeypatch)
    fake_uv = _patch_uv_export(module, monkeypatch, stdout="old==0.1")
    assert _run_main(module, monkeypatch, script_path) == 0
    requirements_path = _only_generated_file(tmp_repo, script_path)
    capsys.readouterr()

    fake_uv.stdout = "new==2.0"
    exit_code = _run_main(module, monkeypatch, script_path, "--check")

    captured = capsys.readouterr()
    diff = captured.out + captured.err
    assert exit_code == 1
    assert len(fake_uv.calls) == 2
    _assert_uv_export_call(fake_uv.calls[1], cwd=tmp_repo)
    assert "-old==0.1" in diff
    assert "+new==2.0" in diff
    assert requirements_path.read_text(encoding="utf-8") == "old==0.1\n"


def test_export_failure_returns_subprocess_code_without_replacing_good_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, tmp_repo, script_path = _load_temp_script(tmp_path, monkeypatch)
    fake_uv = _patch_uv_export(module, monkeypatch, stdout="good==1.0")
    assert _run_main(module, monkeypatch, script_path) == 0
    requirements_path = _only_generated_file(tmp_repo, script_path)

    fake_uv.stdout = "bad==9.9"
    fake_uv.stderr = "uv export failed"
    fake_uv.returncode = 23

    exit_code = _run_main(module, monkeypatch, script_path)

    assert exit_code == 23
    assert len(fake_uv.calls) == 2
    _assert_uv_export_call(fake_uv.calls[1], cwd=tmp_repo)
    assert requirements_path.read_text(encoding="utf-8") == "good==1.0\n"
