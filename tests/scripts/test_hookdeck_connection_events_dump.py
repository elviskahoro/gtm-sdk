"""Unit tests for the Flox-only Hookdeck dump recipe."""

# ruff: noqa: S101, SLF001, PT001, PT018

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "hookdeck-connection_events-dump.py"


@pytest.fixture()
def hd() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hookdeck_dump", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["---", "🎉", "東京"])
def test_slugify_always_returns_a_child_name(hd: ModuleType, name: str) -> None:
    assert hd._slugify(name) == "dump"


def test_slugify_normalizes_safe_names(hd: ModuleType) -> None:
    assert hd._slugify("My Connection!") == "my-connection"


def test_checksum_requires_pinned_digest(hd: ModuleType) -> None:
    with pytest.raises(RuntimeError, match="No pinned checksum"):
        hd._verify_checksum(b"data", "unknown.tar.gz")


def test_checksum_rejects_modified_binary(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(hd._HOOKDECK_CLI_BINARY_CHECKSUMS, "x.tar.gz", "0" * 64)
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        hd._verify_checksum(b"data", "x.tar.gz")


def test_main_uses_flox_recipe_by_default(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOOKDECK_API_KEY", "secret")
    runner = MagicMock()

    def create_output_dir(*, output_dir: Path, **_: object) -> None:
        output_dir.mkdir(parents=True)

    runner.side_effect = create_output_dir
    monkeypatch.setattr(hd, "_dump_via_flox", runner)
    monkeypatch.setattr(
        sys,
        "argv",
        ["dump.py", "--connection-id", "web_x", "--output-dir", str(tmp_path)],
    )

    assert hd.main() == 0
    runner.assert_called_once()


def test_empty_slug_cannot_replace_output_root(hd: ModuleType, tmp_path: Path) -> None:
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep")
    final_dir = tmp_path / hd._slugify("🎉")
    assert final_dir != tmp_path
    assert sibling.exists()


def test_checksum_accepts_matching_digest(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"binary"
    name = "x.tar.gz"
    monkeypatch.setitem(
        hd._HOOKDECK_CLI_BINARY_CHECKSUMS,
        name,
        hashlib.sha256(data).hexdigest(),
    )
    hd._verify_checksum(data, name)
