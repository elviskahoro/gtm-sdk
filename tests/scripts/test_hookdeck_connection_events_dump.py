"""Tests for scripts/hookdeck-connection_events-dump.py.

Mirrors the split used by tests/scripts/test_pr_review_threads.py:

- Pure-logic tests (release-asset mapping, checksum verification) call the
  module's functions directly.
- A mocked-Dagger test asserts the container chain (base image, `hookdeck`
  install, secret injection) and, separately, that it does NOT explicitly set
  OUT_DIR/HD_TMP_DIR -- DUMP_SCRIPT's own defaults must cover the container
  path, or the two executors silently diverge.
- Flox-executor tests patch `subprocess.run` (no real `flox`/`bash`/`hookdeck`
  needed) to assert the argv/env wiring, plus a real-tarfile test of the
  download-and-extract helper.

Neither layer needs a live Dagger/Flox engine, network access, or real
Hookdeck credentials.
"""

# ruff: noqa: S101, SLF001, TRY003, EM101 -- asserts are the point of a test file;
# SLF001 covers deliberate white-box use of the script's private helpers.

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "hookdeck-connection_events-dump.py"
_MODULE_NAME = "_hookdeck_dump_under_test"


@pytest.fixture(scope="module")
def hd() -> Iterator[ModuleType]:
    """Load scripts/hookdeck-connection_events-dump.py without packaging it.

    `scripts/` is excluded from `[tool.setuptools.packages.find]`, so a
    normal import doesn't resolve -- load via file path instead, matching
    tests/scripts/test_pr_review_threads.py's `prt` fixture.
    """
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(_MODULE_NAME, None)


# ---------------------------------------------------------------------------
# Release-asset mapping
# ---------------------------------------------------------------------------


def test_release_asset_maps_darwin_arm64(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hd.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hd.platform, "machine", lambda: "arm64")
    asset = hd._hookdeck_release_asset()
    assert asset == f"hookdeck_{hd.HOOKDECK_CLI_VERSION}_darwin_arm64.tar.gz"


def test_release_asset_maps_linux_arm64(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hd.platform, "machine", lambda: "aarch64")
    asset = hd._hookdeck_release_asset()
    assert asset == f"hookdeck_{hd.HOOKDECK_CLI_VERSION}_linux_arm64.tar.gz"


def test_release_asset_maps_linux_amd64(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hd.platform, "machine", lambda: "x86_64")
    asset = hd._hookdeck_release_asset()
    assert asset == f"hookdeck_{hd.HOOKDECK_CLI_VERSION}_linux_amd64.tar.gz"


def test_release_asset_raises_for_unsupported_platform(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hd.platform, "system", lambda: "Windows")
    monkeypatch.setattr(hd.platform, "machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="No pinned hookdeck-cli"):
        hd._hookdeck_release_asset()


# ---------------------------------------------------------------------------
# Checksum verification
# ---------------------------------------------------------------------------


def test_verify_checksum_passes_for_matching_digest(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"fake binary bytes"
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setitem(hd._HOOKDECK_CLI_BINARY_CHECKSUMS, "some-asset.tar.gz", digest)
    hd._verify_checksum(data, "some-asset.tar.gz")


def test_verify_checksum_raises_on_mismatch(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"fake binary bytes"
    wrong_digest = hashlib.sha256(b"different bytes").hexdigest()
    monkeypatch.setitem(
        hd._HOOKDECK_CLI_BINARY_CHECKSUMS,
        "some-asset.tar.gz",
        wrong_digest,
    )
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        hd._verify_checksum(data, "some-asset.tar.gz")


def test_verify_checksum_raises_when_filename_absent(hd: ModuleType) -> None:
    with pytest.raises(RuntimeError, match="No pinned checksum recorded"):
        hd._verify_checksum(b"data", "unpinned-asset.tar.gz")


# ---------------------------------------------------------------------------
# _ensure_hookdeck_cli_installed -- real tarfile, monkeypatched download
# ---------------------------------------------------------------------------

_FAKE_BINARY_CONTENT = b"#!/bin/sh\necho fake-hookdeck\n"


def _build_fake_release_archive(tmp_path: Path) -> bytes:
    """A real tar.gz containing a `hookdeck` file, matching the real release layout."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    binary_path = tmp_path / "hookdeck"
    binary_path.write_bytes(_FAKE_BINARY_CONTENT)
    archive_path = tmp_path / "archive.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(binary_path, arcname="hookdeck")
    return archive_path.read_bytes()


def test_ensure_hookdeck_cli_installed_downloads_verifies_and_extracts(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive_bytes = _build_fake_release_archive(tmp_path / "src")
    binary_digest = hashlib.sha256(_FAKE_BINARY_CONTENT).hexdigest()
    asset_name = "hookdeck_2.3.1_linux_amd64.tar.gz"
    monkeypatch.setitem(hd._HOOKDECK_CLI_BINARY_CHECKSUMS, asset_name, binary_digest)

    monkeypatch.setattr(hd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hd.platform, "machine", lambda: "x86_64")

    def fake_download(url: str) -> bytes:
        if url.endswith(asset_name):
            return archive_bytes
        msg = f"unexpected URL in test: {url}"
        raise AssertionError(msg)

    monkeypatch.setattr(hd, "_download", fake_download)

    prefix = tmp_path / "prefix"
    result = hd._ensure_hookdeck_cli_installed(prefix)

    assert result == prefix
    binary = prefix / "hookdeck"
    assert binary.exists()
    assert binary.stat().st_mode & 0o111  # executable bit set


def test_ensure_hookdeck_cli_installed_skips_when_cache_verifies_ok(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset_name = "hookdeck_2.3.1_linux_amd64.tar.gz"
    monkeypatch.setattr(hd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hd.platform, "machine", lambda: "x86_64")
    monkeypatch.setitem(
        hd._HOOKDECK_CLI_BINARY_CHECKSUMS,
        asset_name,
        hashlib.sha256(_FAKE_BINARY_CONTENT).hexdigest(),
    )

    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "hookdeck").write_bytes(_FAKE_BINARY_CONTENT)

    def fail_download(url: str) -> bytes:
        msg = f"_download should not be called when the cached binary verifies: {url}"
        raise AssertionError(msg)

    monkeypatch.setattr(hd, "_download", fail_download)

    result = hd._ensure_hookdeck_cli_installed(prefix)
    assert result == prefix


def test_ensure_hookdeck_cli_installed_redownloads_when_cache_is_corrupted(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cached binary that no longer matches its pinned digest must not be
    trusted just because a file exists at the expected path -- it's deleted
    and reinstalled from a fresh, verified download.
    """
    archive_bytes = _build_fake_release_archive(tmp_path / "src")
    binary_digest = hashlib.sha256(_FAKE_BINARY_CONTENT).hexdigest()
    asset_name = "hookdeck_2.3.1_linux_amd64.tar.gz"
    monkeypatch.setitem(hd._HOOKDECK_CLI_BINARY_CHECKSUMS, asset_name, binary_digest)

    monkeypatch.setattr(hd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hd.platform, "machine", lambda: "x86_64")

    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "hookdeck").write_bytes(b"corrupted-or-tampered-content")

    download_calls = []

    def fake_download(url: str) -> bytes:
        download_calls.append(url)
        if url.endswith(asset_name):
            return archive_bytes
        msg = f"unexpected URL in test: {url}"
        raise AssertionError(msg)

    monkeypatch.setattr(hd, "_download", fake_download)

    result = hd._ensure_hookdeck_cli_installed(prefix)

    assert result == prefix
    assert download_calls  # the corrupted cache was not trusted
    assert (prefix / "hookdeck").read_bytes() == _FAKE_BINARY_CONTENT


# ---------------------------------------------------------------------------
# Flox executor -- argv/env wiring, no real flox/bash/hookdeck needed
# ---------------------------------------------------------------------------


def _identity_prefix(prefix: Path) -> Path:
    return prefix


def test_dump_via_flox_wires_flox_activate_and_real_output_dirs(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify wiring, plus the two properties roborev flagged in the shared-dir design.

    - HOOKDECK_CONFIG_FILE stays inside the per-invocation scratch dir, never
      the operator's real `~/.config/hookdeck/config.toml`.
    - the scratch dir is unique per call (not the fixed path a concurrent
      invocation could clobber) and is removed once the call returns.
    """
    monkeypatch.setattr(hd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(hd, "FLOX_HOOKDECK_BIN_PREFIX", tmp_path / "bin")
    monkeypatch.setattr(hd, "_ensure_hookdeck_cli_installed", _identity_prefix)

    captured_script_path: Path | None = None
    captured_env: dict[str, str] | None = None

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        # Capture while the work dir still exists -- it's removed on return.
        del cwd, check
        nonlocal captured_script_path, captured_env
        captured_script_path = Path(argv[-1])
        captured_env = env
        assert captured_script_path.exists()
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hd.subprocess, "run", fake_run)

    output_dir = tmp_path / "out"
    hd._dump_via_flox(
        connection_id="web_x",
        connection_name=None,
        output_dir=output_dir,
        api_key="sekret",
        limit_per_page=100,
        max_events=5,
    )

    assert output_dir.exists()
    assert captured_script_path is not None
    assert captured_env is not None
    env = captured_env
    work_dir = captured_script_path.parent

    assert work_dir.parent == tmp_path / "tmp"
    assert work_dir.name.startswith("hookdeck-dump-work-")

    assert env["HOOKDECK_API_KEY"] == "sekret"
    assert env["HOOKDECK_CONFIG_FILE"] == str(work_dir / "hookdeck-config.toml")
    assert env["CONNECTION_ID"] == "web_x"
    assert env["CONNECTION_NAME"] == ""
    assert env["LIMIT_PER_PAGE"] == "100"
    assert env["MAX_EVENTS"] == "5"
    assert env["OUT_DIR"] == str(output_dir)
    assert env["HD_TMP_DIR"] == str(work_dir / "hd")
    assert str(tmp_path / "bin") in env["PATH"].split(hd.os.pathsep)

    # The per-invocation scratch dir (script, config file, hd/ scratch) is
    # gone once the call returns -- nothing lingers for a concurrent run to
    # collide with, and no config file survives outside tmp/.
    assert not work_dir.exists()


def test_dump_via_flox_cleans_up_work_dir_even_on_failure(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(hd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(hd, "FLOX_HOOKDECK_BIN_PREFIX", tmp_path / "bin")
    monkeypatch.setattr(hd, "_ensure_hookdeck_cli_installed", _identity_prefix)

    captured_work_dir: Path | None = None

    def fake_run_that_fails(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> None:
        del cwd, env, check
        nonlocal captured_work_dir
        captured_work_dir = Path(argv[-1]).parent
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(hd.subprocess, "run", fake_run_that_fails)

    with pytest.raises(subprocess.CalledProcessError):
        hd._dump_via_flox(
            connection_id="web_x",
            connection_name=None,
            output_dir=tmp_path / "out",
            api_key="sekret",
            limit_per_page=100,
            max_events=None,
        )

    assert captured_work_dir is not None
    assert not captured_work_dir.exists()


def test_use_flox_rejects_unrecognized_value(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GTM_HOOKDECK_DUMP_VIA_FLOX", "banana")
    with pytest.raises(ValueError, match="GTM_HOOKDECK_DUMP_VIA_FLOX"):
        hd._use_flox()


# ---------------------------------------------------------------------------
# main() dispatch -- exactly one executor runs, based on the env flag
# ---------------------------------------------------------------------------


def _mkdir_output(**kwargs: object) -> None:
    """Side effect shared by both dispatch tests' fakes.

    The real executors create `output_dir` themselves as their first step;
    `main()` renames that directory afterwards, so a fake that does nothing
    would leave `main()` with no directory to rename.
    """
    output_dir = kwargs["output_dir"]
    assert isinstance(output_dir, Path)
    output_dir.mkdir(parents=True, exist_ok=True)


def test_main_dispatches_to_flox_when_flag_set(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GTM_HOOKDECK_DUMP_VIA_FLOX", "1")
    monkeypatch.setenv("HOOKDECK_API_KEY", "sekret")
    fake_flox = MagicMock(side_effect=_mkdir_output)
    fake_dagger_dump = AsyncMock(side_effect=_mkdir_output)
    monkeypatch.setattr(hd, "_dump_via_flox", fake_flox)
    monkeypatch.setattr(hd, "_dump_via_dagger", fake_dagger_dump)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hookdeck-connection_events-dump.py",
            "--connection-id",
            "web_x",
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )

    hd.main()

    fake_flox.assert_called_once()
    fake_dagger_dump.assert_not_called()


def test_main_dispatches_to_dagger_by_default(
    hd: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("GTM_HOOKDECK_DUMP_VIA_FLOX", raising=False)
    monkeypatch.setenv("HOOKDECK_API_KEY", "sekret")
    fake_flox = MagicMock(side_effect=_mkdir_output)
    fake_dagger_dump = AsyncMock(side_effect=_mkdir_output)
    monkeypatch.setattr(hd, "_dump_via_flox", fake_flox)
    monkeypatch.setattr(hd, "_dump_via_dagger", fake_dagger_dump)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hookdeck-connection_events-dump.py",
            "--connection-id",
            "web_x",
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )

    hd.main()

    fake_dagger_dump.assert_called_once()
    fake_flox.assert_not_called()


# ---------------------------------------------------------------------------
# Mocked-Dagger executor: container chain + no explicit OUT_DIR/HD_TMP_DIR
# ---------------------------------------------------------------------------


def _build_dagger_mock() -> tuple[MagicMock, MagicMock]:
    final_container = MagicMock(name="final_container")
    final_directory = MagicMock(name="final_directory")
    final_directory.export = AsyncMock(return_value=None)
    final_container.directory.return_value = final_directory

    dag = MagicMock(name="dag")
    dag.container.return_value.from_.return_value.with_exec.return_value.with_exec.return_value.with_new_file.return_value = MagicMock(
        name="container_with_script",
    )
    container_with_script = dag.container.return_value.from_.return_value.with_exec.return_value.with_exec.return_value.with_new_file.return_value
    chained = container_with_script.with_secret_variable.return_value
    chained.with_env_variable.return_value = chained
    chained.with_exec.return_value = final_container

    def set_secret(name: str, value: str) -> MagicMock:
        return MagicMock(_secret=(name, value))

    dag.set_secret.side_effect = set_secret

    connection_cm = MagicMock(name="connection_cm")
    connection_cm.__aenter__ = AsyncMock(return_value=None)
    connection_cm.__aexit__ = AsyncMock(return_value=None)

    fake_dagger = MagicMock(name="dagger_module")
    fake_dagger.connection.return_value = connection_cm
    fake_dagger.Config = MagicMock(name="Config")
    fake_dagger.dag = dag

    return fake_dagger, chained


def test_dagger_executor_does_not_set_out_dir_or_hd_tmp_dir(
    hd: ModuleType,
    tmp_path: Path,
) -> None:
    """DUMP_SCRIPT's own defaults must cover the container path.

    If a future edit starts explicitly setting OUT_DIR/HD_TMP_DIR here to
    something other than /out and /tmp/hd, it would silently diverge from
    what the container's own `with_new_file` + `.directory("/out")` export
    step assumes.
    """
    fake_dagger, chained = _build_dagger_mock()

    with patch.object(hd, "dagger", fake_dagger):
        hd.asyncio.run(
            hd._dump_via_dagger(
                connection_id="web_x",
                connection_name=None,
                output_dir=tmp_path / "out",
                api_key="sekret",
                limit_per_page=100,
                max_events=None,
            ),
        )

    env_var_names = {call.args[0] for call in chained.with_env_variable.call_args_list}
    assert "OUT_DIR" not in env_var_names
    assert "HD_TMP_DIR" not in env_var_names
    assert env_var_names == {
        "CONNECTION_ID",
        "CONNECTION_NAME",
        "LIMIT_PER_PAGE",
        "MAX_EVENTS",
    }
