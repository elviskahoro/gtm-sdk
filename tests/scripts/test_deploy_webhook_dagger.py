"""Tests for the shared execution contract used by webhook redeploy."""

# ruff: noqa: S101, SLF001, PLR2004, PT001, PT018
# trunk-ignore-all(bandit/B105): test-only credential fixtures

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

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
    await script_module._deploy_via_dagger(
        handler,
        repo_root=REPO_ROOT,
        deploy_env={"GH_TOKEN": "secret"},
    )

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


def test_isolated_checkout_keeps_placeholder_out_of_host_tree(
    script_module: ModuleType,
) -> None:
    """The temporary deploy checkout is distinct from the operator tree."""
    original = (REPO_ROOT / "webhooks" / "export_to_attio.py").read_text()
    with script_module._isolated_checkout() as checkout:
        isolated = Path(checkout) / "webhooks" / "export_to_attio.py"
        isolated.write_text(
            isolated.read_text().replace("WebhookModelToReplace", "Example"),
        )
        assert "WebhookModelToReplace" not in isolated.read_text()
    assert (REPO_ROOT / "webhooks" / "export_to_attio.py").read_text() == original
