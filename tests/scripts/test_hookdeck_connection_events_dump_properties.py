"""Universal safety properties for the Hookdeck dump path helpers."""

# ruff: noqa: S101, SLF001

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import (
    event,
    given,
    strategies as st,
)

if TYPE_CHECKING:
    from types import ModuleType

# Hypothesis's decorator lacks the strict type metadata pyright needs.
# pyright: reportUntypedFunctionDecorator=false

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "hookdeck-connection_events-dump.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hookdeck_dump_properties",
        SCRIPT_PATH,
    )
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@given(st.text())
def test_slugify_is_always_a_safe_child_name(name: str) -> None:
    hd = _load_module()
    try:
        slug = hd._slugify(name)
    finally:
        sys.modules.pop("hookdeck_dump_properties", None)
    event(f"empty-fallback={slug == 'dump'}")
    assert slug
    assert slug == slug.lower()
    assert "/" not in slug
    assert "\\" not in slug
    assert slug not in {".", ".."}
