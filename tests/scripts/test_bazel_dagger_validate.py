"""Contracts for the local Dagger Bazel runner."""

# ruff: noqa: S101, SLF001, S108 -- direct script contracts use assertions, internals, and literal paths.

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bazel-dagger-validate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_bazel_dagger_validate", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_base_is_origin_main() -> None:
    module = _load()

    assert module.DEFAULT_BASE == "origin/main"


def test_controller_environment_uses_shared_cache_and_omits_trunk_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    monkeypatch.setenv("TRUNK_API_TOKEN", "secret")
    monkeypatch.setenv("TRUNK_REPOSITORY", "owner/repo")
    environment = module._controller_env(
        source_dir=Path("/tmp/source"),  # nosec B108
        toolchain_dir=Path("/tmp/toolchain"),  # nosec B108
        mode="full",
    )

    assert environment["BAZEL_DAGGER_BINARY"] == "/tmp/toolchain/bazel"  # nosec B108
    assert (
        environment["BAZEL_DAGGER_DIFF_JAR"] == "/tmp/toolchain/bazel-diff_deploy.jar"
    )  # nosec B108
    assert environment["BAZEL_DAGGER_CACHE_DIR"] == "/tmp/cache"  # nosec B108
    assert environment["BAZEL_DAGGER_SOURCE_DIR"] == "/tmp/source"  # nosec B108
    assert environment["BAZEL_DAGGER_MODE"] == "full"
    assert environment["DAGGER_NO_NAG"] == "1"
    assert "TRUNK_API_TOKEN" not in environment
    assert "TRUNK_REPOSITORY" not in environment


def test_controller_environment_does_not_forward_unrelated_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    monkeypatch.setenv("UNRELATED_SECRET", "secret")

    environment = module._controller_env(
        source_dir=Path("/tmp/source"),  # nosec B108
        toolchain_dir=Path("/tmp/toolchain"),  # nosec B108
    )

    assert "UNRELATED_SECRET" not in environment
