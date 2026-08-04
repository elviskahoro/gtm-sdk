"""Unit tests for the Flox-only Hookdeck dump recipe."""

# ruff: noqa: S101, SLF001, PT001, PT018

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "hookdeck-connection_events-dump.py"


@pytest.fixture()
def hd() -> Generator[ModuleType]:
    spec = importlib.util.spec_from_file_location("hookdeck_dump", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


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


def test_derived_path_outside_the_root_is_refused(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep")
    root = tmp_path / "out"

    def create_output_dir(*, output_dir: Path, **_: object) -> None:
        output_dir.mkdir(parents=True)
        (output_dir / ".connection_name").write_text("escape")

    monkeypatch.setenv("HOOKDECK_API_KEY", "secret")
    monkeypatch.setattr(hd, "_dump_via_flox", MagicMock(side_effect=create_output_dir))

    def escaping_slug(_name: str) -> str:
        return "../sibling"

    monkeypatch.setattr(hd, "_slugify", escaping_slug)
    monkeypatch.setattr(
        sys,
        "argv",
        ["dump.py", "--connection-id", "web_x", "--output-dir", str(root)],
    )

    with pytest.raises(RuntimeError, match="Refusing to replace"):
        hd.main()
    assert (sibling / "keep.txt").exists()


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


def test_cached_hookdeck_binary_is_reverified_and_reinstalled(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = "x.tar.gz"
    binary = b"verified binary"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        member = tarfile.TarInfo("hookdeck")
        member.size = len(binary)
        tar.addfile(member, io.BytesIO(binary))

    monkeypatch.setattr(hd, "_hookdeck_release_asset", lambda: asset)
    monkeypatch.setitem(
        hd._HOOKDECK_CLI_BINARY_CHECKSUMS,
        asset,
        hashlib.sha256(binary).hexdigest(),
    )
    download = MagicMock(return_value=archive.getvalue())
    monkeypatch.setattr(hd, "_download", download)
    prefix = tmp_path / "cache"
    prefix.mkdir()
    (prefix / "hookdeck").write_bytes(b"tampered")

    replace_calls: list[tuple[Path, Path]] = []
    original_replace = Path.replace

    def replace(source: Path, target: Path) -> Path:
        replace_calls.append((source, target))
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    assert hd._ensure_hookdeck_cli_installed(prefix) == prefix
    assert (prefix / "hookdeck").read_bytes() == binary
    assert (prefix / "hookdeck").stat().st_mode & 0o111
    assert download.call_count == 1
    assert len(replace_calls) == 1

    assert hd._ensure_hookdeck_cli_installed(prefix) == prefix
    assert download.call_count == 1
