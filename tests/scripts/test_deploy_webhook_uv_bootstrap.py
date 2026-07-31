"""Unit tests for `_bootstrap_uv()` in scripts/webhooks-handlers-redeploy.py.

`_bootstrap_uv()` re-execs the process via `os.execv`, which can't be
exercised end-to-end in a test process (it would replace the test runner
itself). Instead, load the module without triggering its bootstrap -- the
`if __name__ == "__main__":` guard means importing it via
`importlib.util.spec_from_file_location` never fires it, the same load path
`tests/scripts/test_deploy_webhook_dagger.py` already relies on -- then call
`_bootstrap_uv()` directly with `os.execv` patched to record its arguments
instead of replacing the process.

Without the `__name__` guard this module's own collection would have
replaced the pytest process the first time this file's `script_module`
fixture ran; without the active-virtualenv fast path,
`tests/scripts/test_deploy_webhook.py`'s `sys.executable`-based invocation
(which deliberately bypasses the shebang so its PATH-stubbed `uv`
intercepts only internal calls) would trigger a real re-exec instead. Both
guards are covered here directly, at the unit level.
"""
# ruff: noqa: S101, SLF001 -- deliberate white-box testing of private internals

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "webhooks-handlers-redeploy.py"

_MODULE_NAME = "_webhooks_redeploy_bootstrap_under_test"


@pytest.fixture
def script_module() -> Iterator[ModuleType]:
    """Load the script fresh per test -- `_bootstrap_uv` mutates `os.environ`."""
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(_MODULE_NAME, None)


class _FakeCandidate:
    def __init__(self, path: str) -> None:
        self.path = path
        self.version = (0, 11, 29)


def test_skips_when_sentinel_already_set(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(script_module._UV_BOOTSTRAP_ENV, "1")
    execv = MagicMock(side_effect=AssertionError("execv should not be called"))
    monkeypatch.setattr(script_module.os, "execv", execv)

    script_module._bootstrap_uv()

    execv.assert_not_called()


def test_skips_when_already_in_project_venv(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(script_module._UV_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(
        script_module.sys,
        "prefix",
        str(script_module.REPO_ROOT / ".venv"),
    )
    monkeypatch.setattr(script_module.sys, "base_prefix", "/usr")
    execv = MagicMock(side_effect=AssertionError("execv should not be called"))
    monkeypatch.setattr(script_module.os, "execv", execv)

    script_module._bootstrap_uv()

    execv.assert_not_called()
    assert script_module.os.environ.get(script_module._UV_BOOTSTRAP_ENV) == "1"


def test_skips_when_already_in_any_virtualenv(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(script_module._UV_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(script_module.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(script_module.sys, "base_prefix", str(tmp_path / "python"))
    execv = MagicMock(side_effect=AssertionError("execv should not be called"))
    monkeypatch.setattr(script_module.os, "execv", execv)

    script_module._bootstrap_uv()

    execv.assert_not_called()
    assert script_module.os.environ.get(script_module._UV_BOOTSTRAP_ENV) == "1"


def test_execs_into_resolved_compatible_uv(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(script_module._UV_BOOTSTRAP_ENV, raising=False)
    # Anywhere that isn't this repo's real .venv.
    monkeypatch.setattr(
        script_module.sys,
        "prefix",
        str(tmp_path / "not-the-real-venv"),
    )
    monkeypatch.setattr(
        script_module.sys,
        "base_prefix",
        str(tmp_path / "not-the-real-venv"),
    )

    def _resolve(cwd: str | None = None) -> _FakeCandidate:
        del cwd
        return _FakeCandidate("/fake/compatible/uv")

    monkeypatch.setattr(script_module, "find_compatible_uv_for_repo", _resolve)
    monkeypatch.setattr(
        script_module.sys,
        "argv",
        [str(SCRIPT_PATH), "export_to_attio", "SomeSource"],
    )
    # execv replaces the process and never returns -- simulate that with a
    # side effect so the test can still assert on how it was called. chdir
    # is mocked too so the test doesn't actually change pytest's real cwd.
    execv = MagicMock(side_effect=SystemExit(0))
    monkeypatch.setattr(script_module.os, "execv", execv)
    chdir = MagicMock()
    monkeypatch.setattr(script_module.os, "chdir", chdir)

    with pytest.raises(SystemExit):
        script_module._bootstrap_uv()

    # chdir must happen before execv -- os.execv has no cwd parameter, so
    # without actually changing directory first, the re-exec'd process
    # would inherit the ambient invocation cwd instead of REPO_ROOT, and a
    # pyenv shim there could dispatch to a different uv than was verified.
    chdir.assert_called_once_with(script_module.REPO_ROOT)
    execv.assert_called_once()
    called_path, called_argv = execv.call_args[0]
    assert called_path == "/fake/compatible/uv"
    assert called_argv == [
        "/fake/compatible/uv",
        "run",
        "--project",
        str(script_module.REPO_ROOT),
        "python",
        str(SCRIPT_PATH.resolve()),
        "export_to_attio",
        "SomeSource",
    ]
    assert script_module.os.environ.get(script_module._UV_BOOTSTRAP_ENV) == "1"


def test_fails_clearly_when_no_compatible_uv_found(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(script_module._UV_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(
        script_module.sys,
        "prefix",
        str(tmp_path / "not-the-real-venv"),
    )
    monkeypatch.setattr(
        script_module.sys,
        "base_prefix",
        str(tmp_path / "not-the-real-venv"),
    )

    def _raise(cwd: str | None = None) -> None:  # noqa: ARG001
        raise script_module.NoCompatibleUvError([], ">=0.11.8,<0.12")

    monkeypatch.setattr(script_module, "find_compatible_uv_for_repo", _raise)
    execv = MagicMock(side_effect=AssertionError("execv should not be called"))
    monkeypatch.setattr(script_module.os, "execv", execv)

    with pytest.raises(SystemExit) as exc_info:
        script_module._bootstrap_uv()

    assert exc_info.value.code == 1
    execv.assert_not_called()
    captured = capsys.readouterr()
    assert ">=0.11.8,<0.12" in captured.err
