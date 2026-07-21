"""Test-first tests for scripts/bazel-requirements-sync.py.

The synchronizer is the single bridge between the pinned ``uv.lock`` and the
hashed ``requirements_bazel.txt`` that Bazel's ``rules_python`` ``pip.parse``
hub will consume (Task 3). These tests pin its contract before the
implementation lands: the exact ``uv export`` argv, ``REPO_ROOT`` cwd
anchoring, write mode, ``--check`` drift detection, and subprocess-failure
propagation that surfaces the uv error instead of silently writing an empty
file.

Each test monkeypatches the module's ``subprocess`` reference so no real
``uv`` binary is invoked — the unit under test is the script's orchestration,
not uv's export logic. The real end-to-end generation is exercised by running
the script for the committed ``requirements_bazel.txt`` (audited separately).
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bazel-requirements-sync.py"


@pytest.fixture  # pyright: ignore[reportUntypedFunctionDecorator]
def sync_module() -> Any:
    """Load the synchronizer by file path — the hyphenated name isn't importable."""
    spec = importlib.util.spec_from_file_location("bazel_requirements_sync", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeSubprocess:
    """Minimal subprocess stand-in: records calls, returns canned CompletedProcess."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.calls: list[dict[str, Any]] = []

    def run(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"args": args, "kwargs": kwargs})
        cmd = args[0] if args else kwargs.get("args", [])
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    mod: Any,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> _FakeSubprocess:
    """Replace the module's ``subprocess`` global with a recording fake."""
    fake = _FakeSubprocess(stdout=stdout, stderr=stderr, returncode=returncode)
    monkeypatch.setattr(mod, "subprocess", fake)
    return fake


def test_export_command_argv(monkeypatch: pytest.MonkeyPatch, sync_module: Any) -> None:
    """EXPORT_COMMAND is the exact minimal argv and is invoked verbatim.

    ``--no-emit-project`` keeps the editable ``gtm`` package out of the Bazel
    requirements (Bazel builds first-party code from source). Hashes and the
    default groups/extras are kept — ``--no-hashes``/``--all-extras`` are out
    of scope.
    """
    assert sync_module.EXPORT_COMMAND == ["uv", "export", "--no-emit-project"]

    fake = _patch_subprocess(monkeypatch, sync_module, stdout="OK\n")
    sync_module._run_export()

    assert len(fake.calls) == 1
    assert fake.calls[0]["args"][0] == sync_module.EXPORT_COMMAND


def test_run_export_anchored_to_repo_root(
    monkeypatch: pytest.MonkeyPatch,
    sync_module: Any,
) -> None:
    """``uv export`` runs with ``cwd=REPO_ROOT``, never the caller's CWD.

    Anchors to the script's own location so ``uv run scripts/...`` from any
    directory resolves ``uv.lock`` at the repo root — the documented
    ``uv run path/to/script.py``-doesn't-chdir footgun.
    """
    fake = _patch_subprocess(monkeypatch, sync_module, stdout="OK\n")
    sync_module._run_export()
    assert fake.calls[0]["kwargs"]["cwd"] == sync_module.REPO_ROOT


def test_write_mode_writes_requirements_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sync_module: Any,
) -> None:
    """Default invocation writes the export stdout to requirements_bazel.txt."""
    target = tmp_path / "requirements_bazel.txt"
    monkeypatch.setattr(sync_module, "REQUIREMENTS_FILE", target)
    payload = "some==1.0 \\\n    --hash=sha256:abc\n"
    _patch_subprocess(monkeypatch, sync_module, stdout=payload)

    assert sync_module.main([]) == 0

    assert target.read_text(encoding="utf-8") == payload


def test_check_passes_when_in_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sync_module: Any,
) -> None:
    """``--check`` exits 0 when the committed file matches a fresh export."""
    target = tmp_path / "requirements_bazel.txt"
    monkeypatch.setattr(sync_module, "REQUIREMENTS_FILE", target)
    payload = "in-sync==1.0 \\\n    --hash=sha256:abc\n"
    target.write_text(payload, encoding="utf-8")
    _patch_subprocess(monkeypatch, sync_module, stdout=payload)

    assert sync_module.main(["--check"]) == 0


def test_check_detects_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    sync_module: Any,
) -> None:
    """``--check`` exits 1 and names the drift when the file is stale."""
    target = tmp_path / "requirements_bazel.txt"
    monkeypatch.setattr(sync_module, "REQUIREMENTS_FILE", target)
    target.write_text("stale==0.9\n", encoding="utf-8")
    _patch_subprocess(monkeypatch, sync_module, stdout="fresh==1.0\n")

    assert sync_module.main(["--check"]) == 1

    err = capsys.readouterr().err
    assert "sync" in err.lower() or "drift" in err.lower()


def test_check_fails_when_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    sync_module: Any,
) -> None:
    """``--check`` exits 1 with a clear pointer when the file doesn't exist yet."""
    target = tmp_path / "requirements_bazel.txt"
    monkeypatch.setattr(sync_module, "REQUIREMENTS_FILE", target)
    _patch_subprocess(monkeypatch, sync_module, stdout="fresh==1.0\n")

    assert sync_module.main(["--check"]) == 1

    assert "missing" in capsys.readouterr().err.lower()


def test_subprocess_failure_propagates_and_preserves_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sync_module: Any,
) -> None:
    """When ``uv export`` fails, the script exits non-zero and surfaces uv's stderr.

    "Preserves good output on failure": the error is not swallowed — uv's
    stderr is forwarded to the operator rather than producing a silent empty
    file.
    """
    _patch_subprocess(
        monkeypatch,
        sync_module,
        stdout="",
        stderr="uv: error: failed to read uv.lock\n",
        returncode=1,
    )

    with pytest.raises(SystemExit) as exc_info:
        sync_module._run_export()

    assert exc_info.value.code == 1
    assert "uv: error: failed to read uv.lock" in capsys.readouterr().err


def test_subprocess_failure_does_not_write_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    sync_module: Any,
) -> None:
    """A failed export never leaves a partial/empty requirements file behind."""
    target = tmp_path / "requirements_bazel.txt"
    monkeypatch.setattr(sync_module, "REQUIREMENTS_FILE", target)
    _patch_subprocess(
        monkeypatch,
        sync_module,
        stdout="",
        stderr="boom\n",
        returncode=2,
    )

    with pytest.raises(SystemExit) as exc_info:
        sync_module.main([])

    assert exc_info.value.code == 2
    assert not target.exists()
    assert "boom" in capsys.readouterr().err
