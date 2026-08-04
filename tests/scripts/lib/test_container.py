"""Tests for the shared Dagger transport boundary."""

# ruff: noqa: S101, SLF001

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scripts.lib import container


def test_container_phase_truth_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both markers are finite selectors, so enumerate all states."""
    for phase, flox_env, expected in (
        (False, False, False),
        (False, True, False),
        (True, False, None),
        (True, True, True),
    ):
        for name, enabled in (
            (container.CONTAINER_PHASE, phase),
            ("FLOX_ENV", flox_env),
        ):
            if enabled:
                monkeypatch.setenv(name, "1")
            else:
                monkeypatch.delenv(name, raising=False)
        if expected is None:
            with pytest.raises(RuntimeError, match="activated Flox"):
                container.in_container_phase()
        else:
            assert container.in_container_phase() is expected


def test_recipe_rejects_misaligned_command_secrets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one mapping per command"):
        asyncio.run(
            container.run_recipe_in_container_async(
                repo_root=tmp_path,
                commands=[["first"], ["second"]],
                command_secrets=[{}],
            ),
        )
