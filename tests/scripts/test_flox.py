"""Tests for Flox activation helpers used by script executors."""

# ruff: noqa: S101

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from scripts.lib import flox

if TYPE_CHECKING:
    from pathlib import Path


def _find_flox(_name: str) -> str:
    return "/tools/flox"


def test_run_resolves_flox_before_scrubbing_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", "/operator-home")
    monkeypatch.setattr(flox.shutil, "which", _find_flox)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(flox.subprocess, "run", run)
    flox.run(["uv", "sync"], repo_root=tmp_path, env={"DEPLOY": "1"}, clear_env=True)

    argv, kwargs = calls[0]
    assert argv[:2] == ["/tools/flox", "activate"]
    assert kwargs["env"] == {"HOME": "/operator-home", "DEPLOY": "1"}


def test_preflight_uses_stable_missing_environment_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(flox.shutil, "which", _find_flox)

    def run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(
        flox.subprocess,
        "run",
        run,
    )

    with pytest.raises(flox.FloxEnvironmentNotActivatedError):
        flox.preflight(tmp_path, ("uv",))
