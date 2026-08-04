"""Tests for the shared execution contract used by webhook redeploy."""

# ruff: noqa: S101, SLF001, PLR2004, PT001, PT018
# trunk-ignore-all(bandit/B105): test-only credential fixtures

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "webhooks-handlers-redeploy.py"


@pytest.fixture()
def script_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("webhooks_redeploy", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_deploy_steps_are_the_single_recipe(script_module: ModuleType) -> None:
    steps = script_module.deploy_steps("webhooks/export_to_attio.py")
    assert [step.argv for step in steps] == [
        ["uv", "sync", "--frozen"],
        ["uv", "run", "--no-sync", "modal", "deploy", "webhooks/export_to_attio.py"],
    ]
    assert [step.with_credentials for step in steps] == [False, True]


def test_flox_is_primary_unless_dagger_is_requested(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUN_WITH_DAGGER", raising=False)
    monkeypatch.delenv("CONTAINER_PHASE", raising=False)
    assert script_module._use_flox() is True
    monkeypatch.setenv("RUN_WITH_DAGGER", "1")
    assert script_module._use_flox() is False


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
