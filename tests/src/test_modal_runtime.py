# ruff: noqa: INP001, S101, SLF001 -- focused regression tests for module identity
from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

ENDPOINT_MODULE_NAMES = (
    "src.apollo.organizations",
    "src.apollo.people",
    "src.attio.companies",
    "src.attio.enrichment",
    "src.attio.notes",
    "src.attio.people",
    "src.exa.companies",
    "src.exa.people",
    "src.exa.search",
    "src.accounts.accounts",
    "src.accounts.batch",
    "src.accounts.people",
    "src.accounts.research",
    "src.parallel.extract",
    "src.parallel.findall",
    "src.parallel.search",
)
ENDPOINT_MODULE_COUNT = len(ENDPOINT_MODULE_NAMES)


def _noop_source(_source: str) -> None:
    return None


@pytest.fixture
def modal_modules(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType]:
    monkeypatch.setattr("libs.logging.structured.set_source", _noop_source)
    monkeypatch.setattr("libs.telemetry.init_log_exporter", _noop_source)

    runtime = importlib.import_module("src.modal_runtime")
    app_module = importlib.import_module("src.app")
    return runtime, app_module


def test_app_reexports_modal_runtime_identity(
    modal_modules: tuple[ModuleType, ModuleType],
) -> None:
    runtime, app_module = modal_modules

    assert runtime.app is app_module.app
    assert runtime.image is app_module.image


def test_app_import_registers_expected_endpoint_modules(
    modal_modules: tuple[ModuleType, ModuleType],
) -> None:
    _, app_module = modal_modules

    endpoint_modules = app_module._ENDPOINT_MODULES
    assert endpoint_modules is app_module._REGISTERED_ENDPOINT_MODULES
    assert len(endpoint_modules) == ENDPOINT_MODULE_COUNT
    assert (
        tuple(module.__name__ for module in endpoint_modules) == ENDPOINT_MODULE_NAMES
    )


def test_debug_ping_still_runs_locally(
    modal_modules: tuple[ModuleType, ModuleType],
) -> None:
    _, app_module = modal_modules

    assert app_module.debug_ping.local() == "pong"


def test_endpoint_modules_import_neutral_modal_runtime() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    for module_name in ENDPOINT_MODULE_NAMES:
        path = repo_root.joinpath(*module_name.split(".")).with_suffix(".py")
        tree = ast.parse(path.read_text(), filename=str(path))
        import_froms = [
            node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]

        assert not any(node.module == "src.app" for node in import_froms), module_name
        runtime_imports = [
            node for node in import_froms if node.module == "src.modal_runtime"
        ]
        assert len(runtime_imports) == 1, module_name
        assert {alias.name for alias in runtime_imports[0].names} == {"app", "image"}
