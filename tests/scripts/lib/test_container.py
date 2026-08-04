"""Prevent regressions at the Dagger transport trust boundary."""

# These tests intentionally execute direct imports, assertions, private helpers,
# and typing-only imports to verify the transport boundary contract.
# ruff: noqa: INP001, S101, SLF001, TC003

from __future__ import annotations

import asyncio
import hashlib
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
        if phase and flox_env:
            monkeypatch.setenv(container.EXPECTED_MANIFEST_LOCK_SHA256, "a" * 64)
            monkeypatch.setenv(container.BAKED_MANIFEST_LOCK_SHA256, "a" * 64)
        else:
            monkeypatch.delenv(container.EXPECTED_MANIFEST_LOCK_SHA256, raising=False)
            monkeypatch.delenv(container.BAKED_MANIFEST_LOCK_SHA256, raising=False)
        if expected is None:
            with pytest.raises(RuntimeError, match="activated Flox"):
                container.in_container_phase()
        else:
            assert container.in_container_phase() is expected


@pytest.mark.parametrize(
    ("expected", "baked", "message"),
    [
        ("", "a" * 64, "provenance"),
        ("a" * 64, "", "provenance"),
        ("a" * 64, "b" * 64, "does not match"),
    ],
)
def test_container_phase_rejects_untrusted_manifest(
    monkeypatch: pytest.MonkeyPatch,
    expected: str,
    baked: str,
    message: str,
) -> None:
    """A forged or stale image must not cross the container phase boundary."""
    monkeypatch.setenv(container.CONTAINER_PHASE, "1")
    monkeypatch.setenv("FLOX_ENV", "/nix/store/flox")
    monkeypatch.setenv(container.EXPECTED_MANIFEST_LOCK_SHA256, expected)
    monkeypatch.setenv(container.BAKED_MANIFEST_LOCK_SHA256, baked)
    with pytest.raises(RuntimeError, match=message):
        container.in_container_phase()


@pytest.mark.asyncio
async def test_recipe_passes_manifest_digest_to_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The wrapper derives provenance from the checked-out toolchain lock."""
    lock_path = tmp_path / "flox" / "toolchain" / ".flox" / "env"
    lock_path.mkdir(parents=True)
    lock = lock_path / "manifest.lock"
    lock.write_bytes(b"locked toolchain")
    captured: dict[str, object] = {}

    async def fake_run(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(container, "_run_in_container", fake_run)
    await container.run_recipe_in_container_async(
        repo_root=tmp_path,
        commands=[["true"]],
    )
    assert captured["env"] == {
        container.EXPECTED_MANIFEST_LOCK_SHA256: hashlib.sha256(
            b"locked toolchain",
        ).hexdigest(),
    }


def test_recipe_rejects_misaligned_command_secrets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one mapping per command"):
        asyncio.run(
            container.run_recipe_in_container_async(
                repo_root=tmp_path,
                commands=[["first"], ["second"]],
                command_secrets=[{}],
            ),
        )
