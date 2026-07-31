"""Tests for the generated webhook registry schema."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from typing import Any

import pytest
from pydantic import ValidationError

from cli.webhook.registry import Registry

sync_module = import_module("cli.webhook.sync")


def test_registry_rejects_stale_singletons_field() -> None:
    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "webhooks": [],
        "singletons": [],
    }

    with pytest.raises(ValidationError, match="singletons"):
        Registry.model_validate(payload)


def test_build_registry_has_only_per_source_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Model:
        @staticmethod
        def app_name_for(_handler: str) -> str:
            return "test-app"

    class _HookdeckInventory:
        def find_by_modal_url(self, _modal_url: str) -> tuple[None, None, None]:
            return None, None, None

    monkeypatch.setattr(sync_module, "list_deployed_app_names", set)
    monkeypatch.setattr(sync_module, "fetch_inventory", _HookdeckInventory)
    monkeypatch.setattr(sync_module, "SOURCES", [("test", _Model, "TestModel")])
    monkeypatch.setattr(sync_module, "HANDLERS", [])

    registry = sync_module.build_registry()
    payload = registry.model_dump(mode="json")

    if "singletons" in payload:
        pytest.fail("generated registry still contains the stale singletons field")
    if payload["webhooks"] != [
        {"source": "test", "model": "TestModel", "handlers": []},
    ]:
        pytest.fail("generated registry did not contain the expected per-source rows")
