"""Tests for the shared Dagger transport boundary."""

# ruff: noqa: S101, SLF001

from __future__ import annotations

import pytest

from scripts.lib import container


def test_container_phase_requires_flox_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(container.CONTAINER_PHASE, "1")
    monkeypatch.delenv("FLOX_ENV", raising=False)
    with pytest.raises(RuntimeError, match="activated Flox"):
        container.in_container_phase()


def test_container_phase_is_false_for_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(container.CONTAINER_PHASE, raising=False)
    monkeypatch.delenv("FLOX_ENV", raising=False)
    assert container.in_container_phase() is False
