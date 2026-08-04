"""Tests for the shared execution contract used by webhook redeploy."""

# ruff: noqa: S101, SLF001, PLR2004, PT001, PT018
# trunk-ignore-all(bandit/B105): test-only credential fixtures

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "webhooks-handlers-redeploy.py"


@pytest.fixture()
def script_module() -> Generator[ModuleType]:
    spec = importlib.util.spec_from_file_location("webhooks_redeploy", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def test_deploy_steps_are_the_single_recipe(script_module: ModuleType) -> None:
    steps = script_module.deploy_steps("webhooks/export_to_attio.py")
    assert [step.argv for step in steps] == [
        ["uv", "sync", "--frozen"],
        ["uv", "run", "--no-sync", "modal", "deploy", "webhooks/export_to_attio.py"],
    ]
    assert [step.with_credentials for step in steps] == [False, True]


def test_transport_selector_truth_table(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for dagger, phase, use_dagger, needs_preflight in (
        (False, False, False, True),
        (True, False, True, False),
        (False, True, False, False),
        (True, True, False, False),
    ):
        for name, enabled in (
            ("RUN_WITH_DAGGER", dagger),
            ("CONTAINER_PHASE", phase),
            ("FLOX_ENV", phase),
        ):
            if enabled:
                monkeypatch.setenv(name, "1")
            else:
                monkeypatch.delenv(name, raising=False)
        for name in ("FLOX_MANIFEST_LOCK_SHA256", "FLOX_TOOLCHAIN_MANIFEST_SHA256"):
            if phase:
                monkeypatch.setenv(name, "a" * 64)
            else:
                monkeypatch.delenv(name, raising=False)
        assert script_module._use_dagger() is use_dagger
        assert script_module._needs_flox_preflight() is needs_preflight


@pytest.mark.asyncio
async def test_dagger_transport_keeps_recipe_in_one_container(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = AsyncMock()
    monkeypatch.setattr(script_module, "run_recipe_in_container_async", wrapper)
    handler = REPO_ROOT / "webhooks" / "export_to_attio.py"
    await script_module._deploy_via_dagger(handler, deploy_env={"GH_TOKEN": "secret"})

    assert wrapper.await_count == 1
    call = wrapper.await_args
    assert call is not None
    assert call.kwargs["commands"] == [
        ["uv", "sync", "--frozen"],
        ["uv", "run", "--no-sync", "modal", "deploy", "webhooks/export_to_attio.py"],
    ]
    assert call.kwargs["command_secrets"] == [{}, {"GH_TOKEN": "secret"}]


def test_secret_scrub_surface_remains_explicit(script_module: ModuleType) -> None:
    keys = script_module.deploy_env_scrub_keys()
    assert "MODAL_TOKEN_ID" in keys
    assert "INFISICAL_TOKEN" in keys
    assert "UV_PROJECT_ENVIRONMENT" not in keys


def test_container_phase_cannot_reach_host_cleanup(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container re-entry must fail before lock, backup, or signal setup."""
    monkeypatch.setenv("CONTAINER_PHASE", "1")
    monkeypatch.setenv("FLOX_ENV", "/nix/store/flox")
    monkeypatch.setenv(script_module.EXPECTED_MANIFEST_LOCK_SHA256, "a" * 64)
    monkeypatch.setenv(script_module.BAKED_MANIFEST_LOCK_SHA256, "a" * 64)

    host_only = {
        name: Mock(side_effect=AssertionError(f"host cleanup reached: {name}"))
        for name in ("_acquire_lock", "_write_backup", "_verify_clean_restore")
    }
    for name, replacement in host_only.items():
        monkeypatch.setattr(script_module, name, replacement)
    monkeypatch.setattr(script_module.atexit, "register", host_only["_acquire_lock"])
    monkeypatch.setattr(script_module.signal, "signal", host_only["_write_backup"])

    with pytest.raises(SystemExit, match="1"):
        script_module.main()
    for replacement in host_only.values():
        replacement.assert_not_called()
