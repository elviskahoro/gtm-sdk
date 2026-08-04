"""Unit tests for the shared Bazel Dagger tool cache bootstrap."""

# ruff: noqa: S101 -- direct assertions keep this script contract concise.

from __future__ import annotations

import hashlib
import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bazel-dagger-toolchain.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_bazel_dagger_toolchain", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_existing_valid_artifacts_are_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    bazel = tmp_path / "bazel"
    diff = tmp_path / "bazel-diff_deploy.jar"
    bazel.write_bytes(b"bazel")
    diff.write_bytes(b"diff")
    monkeypatch.setattr(module, "BAZEL_SHA256", hashlib.sha256(b"bazel").hexdigest())
    monkeypatch.setattr(
        module,
        "BAZEL_DIFF_SHA256",
        hashlib.sha256(b"diff").hexdigest(),
    )
    monkeypatch.setattr(
        module,
        "_download",
        lambda *_: pytest.fail("should reuse cache"),
    )

    toolchain = module.ensure_toolchain(tmp_path)

    assert toolchain.bazel == bazel
    assert toolchain.bazel_diff == diff


def test_dagger_bazel_pin_matches_flox_manifest() -> None:
    module = _load()
    manifest = tomllib.loads(
        (REPO_ROOT / ".flox" / "env" / "manifest.toml").read_text(),
    )
    bazel = manifest["install"]["bazel_7"]

    assert bazel["version"] == module.BAZEL_VERSION
    assert bazel["pkg-path"] == "bazel_7"
    assert module.BAZEL_VERSION in module.BAZEL_URL
    assert module.BAZEL_URL.endswith(
        f"bazel-{module.BAZEL_VERSION}-linux-arm64",
    )
    assert module.BAZEL_SHA256 == (
        "e22a8de701585193d886a29acad965f7070db5a98b44d2fc22fc44e65da9e7b5"
    )


def test_corrupt_artifact_is_replaced_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    (tmp_path / "bazel").write_bytes(b"corrupt")
    payloads = {module.BAZEL_URL: b"bazel", module.BAZEL_DIFF_URL: b"diff"}
    monkeypatch.setattr(module, "BAZEL_SHA256", hashlib.sha256(b"bazel").hexdigest())
    monkeypatch.setattr(
        module,
        "BAZEL_DIFF_SHA256",
        hashlib.sha256(b"diff").hexdigest(),
    )
    monkeypatch.setattr(
        module,
        "_download",
        lambda url, destination: destination.write_bytes(payloads[url]),
    )

    toolchain = module.ensure_toolchain(tmp_path)

    assert toolchain.bazel.read_bytes() == b"bazel"
    assert toolchain.bazel_diff.read_bytes() == b"diff"
    assert toolchain.bazel.stat().st_mode & 0o100


def test_checksum_mismatch_fails_without_publishing_bad_cache_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    monkeypatch.setattr(
        module,
        "_download",
        lambda _url, destination: destination.write_bytes(b"bad"),
    )

    with pytest.raises(RuntimeError, match="checksum verification failed"):
        module.ensure_toolchain(tmp_path)

    assert not (tmp_path / "bazel").exists()


def test_checksum_mismatch_preserves_existing_cache_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    bazel = tmp_path / "bazel"
    bazel.write_bytes(b"old-corrupt-cache")
    monkeypatch.setattr(
        module,
        "_download",
        lambda _url, destination: destination.write_bytes(b"still-bad"),
    )

    with pytest.raises(RuntimeError, match="checksum verification failed"):
        module.ensure_toolchain(tmp_path)

    assert bazel.read_bytes() == b"old-corrupt-cache"
    assert list(tmp_path.glob("tmp*")) == []
