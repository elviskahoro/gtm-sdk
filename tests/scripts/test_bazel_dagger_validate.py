"""Contracts for the local Dagger Bazel runner."""

# ruff: noqa: S101, SLF001, S108 -- direct script contracts use assertions, internals, and literal paths.

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

import pytest  # noqa: TC002

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bazel-dagger-validate.py"
GIT_FAILURE = 128


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


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (
            "git@github.com:owner/repo.git",
            "https://github.com/owner/repo.git",
        ),
        (
            "ssh://git@github.com/owner/repo.git",
            "https://github.com/owner/repo.git",
        ),
        (
            "https://github.com/owner/repo.git",
            "https://github.com/owner/repo.git",
        ),
    ],
)
def test_public_origin_url_normalizes_github_remotes(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    expected: str,
) -> None:
    module = _load()

    class Completed:
        stdout = f"{origin}\n"

    def fake_run(*_args: object, **_kwargs: object) -> Completed:
        return Completed()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module._public_origin_url() == expected


def test_main_preserves_unresolved_base_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load()

    def raise_bad_ref(*_args: object, **_kwargs: object) -> int:
        raise subprocess.CalledProcessError(
            GIT_FAILURE,
            ["git"],
            stderr="fatal: bad ref\n",
        )

    monkeypatch.setattr(module, "run", raise_bad_ref)

    assert module.main(["--base", "missing"]) == GIT_FAILURE
    assert "fatal: bad ref" in capsys.readouterr().err


def test_controller_environment_uses_shared_cache_and_omits_trunk_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    monkeypatch.setenv("TRUNK_API_TOKEN", "secret")
    monkeypatch.setenv("TRUNK_REPOSITORY", "owner/repo")
    environment = module._controller_env(
        source_dir=Path("/tmp/source"),  # nosec B108
        toolchain_dir=Path("/tmp/toolchain"),  # nosec B108
        run_impacted=True,
    )

    assert environment["BAZEL_DAGGER_BINARY"] == "/tmp/toolchain/bazel"  # nosec B108
    assert (
        environment["BAZEL_DAGGER_DIFF_JAR"] == "/tmp/toolchain/bazel-diff_deploy.jar"  # nosec B108
    )  # nosec B108
    assert environment["BAZEL_DAGGER_CACHE_DIR"] == "/tmp/cache"  # nosec B108
    assert environment["BAZEL_DAGGER_SOURCE_DIR"] == "/tmp/source"  # nosec B108
    assert environment["BAZEL_RUN_IMPACTED"] == "true"
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


def test_controller_environment_forwards_explicit_ghcr_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    monkeypatch.setenv("GHCR_USERNAME", "octocat")
    monkeypatch.setenv("GHCR_TOKEN", "secret")  # noqa: S105 -- test credential

    environment = module._controller_env(
        source_dir=Path("/tmp/source"),  # nosec B108
        toolchain_dir=Path("/tmp/toolchain"),  # nosec B108
    )

    assert environment["GHCR_USERNAME"] == "octocat"
    assert environment["GHCR_TOKEN"] == "secret"  # noqa: S105 -- test credential
