"""Tests for scripts/beads-rig-sync_to.py.

Three regressions are pinned here, all found by running the script against the
live Gas Town rig after months of drift:

1. ``bd export`` writes JSONL to stdout, so the export step must pass ``-o``.
   The original relied on a bare ``bd export`` refreshing ``.beads/issues.jsonl``
   and died with ``FileNotFoundError`` on a file this repo never writes
   (``export.auto: false``).
2. The default town root moved out of ``~/Documents/ai/town``. Resolution now
   probes candidates instead of hard-coding one dead path.
3. A failing ``bd`` produced a bare ``CalledProcessError`` traceback with bd's
   actual diagnostic (Dolt server down, database missing) trapped in a captured
   attribute. It is now re-printed and the script exits 1.

``bd`` is stubbed on PATH throughout — no test touches a real Dolt server.
"""
# trunk-ignore-all(bandit/B607): `git` resolved via PATH on purpose, argv list, no shell

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "beads-rig-sync_to.py"


def _load_script_module():  # noqa: ANN202 — module object, mirrors sibling tests
    spec = importlib.util.spec_from_file_location("beads_rig_sync_to", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync = _load_script_module()


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_bd_stub(bin_dir: Path, call_log: Path) -> None:
    """Stub `bd` that records argv and honors `export -o <path>`."""
    stub = bin_dir / "bd"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{call_log}"\n'
        'if [ "$1" = "export" ]; then\n'
        "  out=\n"
        "  while [ $# -gt 0 ]; do\n"
        '    if [ "$1" = "-o" ]; then out="$2"; fi\n'
        "    shift\n"
        "  done\n"
        '  printf \'{"id":"gtm-1"}\\n{"id":"gtm-2"}\\n\' > "$out"\n'
        'elif [ "$1" = "config" ]; then\n'
        "  echo false\n"
        'elif [ "$1" = "import" ]; then\n'
        "  echo 'Would import 2 issues' >&2\n"
        "fi\n"
        "exit 0\n",
    )
    _make_executable(stub)


def _write_failing_bd_stub(bin_dir: Path) -> None:
    """Stub `bd` whose export succeeds but whose import fails like a dead rig."""
    stub = bin_dir / "bd"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "export" ]; then\n'
        "  out=\n"
        "  while [ $# -gt 0 ]; do\n"
        '    if [ "$1" = "-o" ]; then out="$2"; fi\n'
        "    shift\n"
        "  done\n"
        '  printf \'{"id":"gtm-1"}\\n\' > "$out"\n'
        "  exit 0\n"
        "fi\n"
        "echo 'Error: failed to open database: database \"gtm_sdk\" not found' >&2\n"
        "echo 'Common causes:' >&2\n"
        "echo '  - The server is serving a different data directory' >&2\n"
        "exit 1\n",
    )
    _make_executable(stub)


@pytest.fixture
def fake_rig(tmp_path: Path) -> Path:
    """A directory shaped like a rig checkout, enough to pass the is_dir gate."""
    rig_beads = tmp_path / "town" / "gtm_sdk" / ".beads"
    rig_beads.mkdir(parents=True)
    return rig_beads


@pytest.fixture
def fake_source_beads(tmp_path: Path) -> Path:
    """A synthetic source ``.beads`` dir, standing in for the real (gitignored,
    symlinked) one that only exists on a developer's primary checkout — not in
    a fresh CI clone. Passed via ``--source-beads`` so tests never depend on
    the real filesystem walk-up.
    """
    source_beads = tmp_path / "src" / ".beads"
    source_beads.mkdir(parents=True)
    return source_beads


def _run_script(
    args: list[str],
    *,
    bin_dir: Path,
) -> subprocess.CompletedProcess[str]:
    env_path = f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin"
    # Bazel's pytest launcher adds declared wheel runfiles directly to sys.path
    # instead of exporting PYTHONPATH. Pass that import path to the child using
    # the same interpreter while continuing to isolate every other ambient
    # variable from the subprocess behavior under test.
    pythonpath = os.pathsep.join(path for path in sys.path if path)
    env = {
        "PATH": env_path,
        "HOME": str(bin_dir.parent),
        "PYTHONPATH": pythonpath,
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


# --- rig location resolution ------------------------------------------------


def test_resolve_rig_beads_prefers_first_existing_candidate_town(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GT_TOWN_ROOT", raising=False)
    stale = tmp_path / "ai" / "town"
    current = tmp_path / "town"
    (current / "gtm_sdk" / ".beads").mkdir(parents=True)
    # Candidate order is preference, not precedence: the stale town is listed
    # first here and must still lose, because it holds no rig.
    monkeypatch.setattr(sync, "TOWN_ROOT_CANDIDATES", (stale, current))

    assert sync.resolve_rig_beads(None) == (current / "gtm_sdk" / ".beads").resolve()


def test_resolve_rig_beads_falls_back_to_preferred_candidate_when_no_town_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GT_TOWN_ROOT", raising=False)
    preferred = tmp_path / "town"
    legacy = tmp_path / "ai" / "town"
    monkeypatch.setattr(sync, "TOWN_ROOT_CANDIDATES", (preferred, legacy))

    # Nothing exists, so the "rig beads dir not found" message should name the
    # modern layout rather than a path the operator has never had.
    assert sync.resolve_rig_beads(None) == (preferred / "gtm_sdk" / ".beads").resolve()


def test_resolve_rig_beads_honors_gt_town_root_even_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GT_TOWN_ROOT", str(tmp_path / "nope"))
    monkeypatch.setattr(sync, "TOWN_ROOT_CANDIDATES", (tmp_path / "town",))

    resolved = sync.resolve_rig_beads(None)
    assert resolved == (tmp_path / "nope" / "gtm_sdk" / ".beads").resolve()


def test_resolve_rig_beads_override_beats_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GT_TOWN_ROOT", str(tmp_path / "env-town"))
    override = tmp_path / "explicit" / ".beads"

    assert sync.resolve_rig_beads(str(override)) == override.resolve()


# --- bd invocation ----------------------------------------------------------


def test_export_passes_output_path_and_includes_memories(
    tmp_path: Path,
    fake_rig: Path,
    fake_source_beads: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.txt"
    _write_bd_stub(bin_dir, call_log)

    result = _run_script(
        [
            "--dry-run",
            "--rig-beads",
            str(fake_rig),
            "--source-beads",
            str(fake_source_beads),
        ],
        bin_dir=bin_dir,
    )

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text().splitlines()
    export_call = next(c for c in calls if c.startswith("export"))
    # -o is the whole point: without it bd streams to stdout and the import
    # step reads a file that was never written.
    assert "-o" in export_call
    assert str(sync.EXPORT_PATH) in export_call
    assert "--include-memories" in export_call
    import_call = next(c for c in calls if c.startswith("import"))
    assert str(sync.EXPORT_PATH) in import_call
    assert "--dry-run" in import_call


def test_export_lands_in_gitignored_tmp_not_beads(tmp_path: Path) -> None:
    # Writing into .beads/ would clobber the source DB's own passive export.
    assert sync.EXPORT_PATH.parent == REPO_ROOT / "tmp"
    ignored = subprocess.run(
        ["git", "check-ignore", str(sync.EXPORT_PATH)],  # noqa: S607 — PATH lookup intended
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0, "export path must be gitignored"


def test_dry_run_does_not_write_rig_config(
    tmp_path: Path,
    fake_rig: Path,
    fake_source_beads: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.txt"
    _write_bd_stub(bin_dir, call_log)

    _run_script(
        [
            "--dry-run",
            "--rig-beads",
            str(fake_rig),
            "--source-beads",
            str(fake_source_beads),
        ],
        bin_dir=bin_dir,
    )

    assert "config set" not in call_log.read_text()


def test_bd_failure_surfaces_diagnostic_without_traceback(
    tmp_path: Path,
    fake_rig: Path,
    fake_source_beads: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_failing_bd_stub(bin_dir)

    result = _run_script(
        [
            "--dry-run",
            "--rig-beads",
            str(fake_rig),
            "--source-beads",
            str(fake_source_beads),
        ],
        bin_dir=bin_dir,
    )

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "CalledProcessError" not in result.stderr
    # bd's own text is the only thing that identifies a stopped Dolt server or a
    # missing database, so it has to reach the operator verbatim.
    assert 'database "gtm_sdk" not found' in result.stderr
    assert "Common causes:" in result.stderr


def test_missing_rig_dir_names_the_path_and_the_listing_command(
    tmp_path: Path,
    fake_source_beads: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_bd_stub(bin_dir, tmp_path / "calls.txt")
    missing = tmp_path / "town" / "gtm_sdk" / ".beads"

    result = _run_script(
        ["--rig-beads", str(missing), "--source-beads", str(fake_source_beads)],
        bin_dir=bin_dir,
    )

    assert result.returncode == 1
    assert str(missing) in result.stderr
    # `gtown` is not a real binary; the CLI is `gastown` (aliased to `gt`).
    assert "gastown rig list" in result.stderr
    assert "gtown rig list" not in result.stderr
