"""Regression tests for direct-script compatible-``uv`` bootstrapping."""
# ruff: noqa: S101, SLF001 -- deliberate white-box coverage of process re-exec

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.lib import uv_bootstrap, uv_resolve

REPO_ROOT = Path(__file__).resolve().parents[2]

PYTHON_ENTRYPOINTS = frozenset(
    {
        "agent-skills-sync.py",
        "attio-meeting_relationship-inspect.py",
        "attio-meetings-find_orphan.py",
        "attio-people-bootstrap.py",
        "attio-social_mentions-bootstrap.py",
        "attio-tracking_events-bootstrap.py",
        "attio-workspace_slug-probe.py",
        "attio-workspace_url-emit.py",
        "beads-rig-sync_to.py",
        "caldotcom-bookings-backfill.py",
        "docs-cli_reference-generate.py",
        "fathom-attio_meetings-backfill.py",
        "fireflies-attio_meetings-backfill.py",
        "hookdeck-connection_events-dump.py",
        "hookdeck-webhook-wire.py",
        "octolens-mentions-backfill.py",
        "rb2b-event_ids-reconcile.py",
        "rb2b-visits-backfill.py",
    },
)
PYTHON_ENTRYPOINT_COUNT = 18
SCRIPT_ENTRYPOINTS = frozenset(
    {
        "ci-triage-diagnose.py",
        "ci-triage-linear-issue.py",
        "docs-pages-lint.py",
        "downstream-contract-sync.py",
        "entire-hooks-setup.py",
    },
)
SCRIPT_ENTRYPOINT_COUNT = 5


def test_all_former_uv_shebang_entrypoints_use_the_shared_bootstrap() -> None:
    """Keep the deferred rollout complete as new direct scripts are added."""
    assert len(PYTHON_ENTRYPOINTS) == PYTHON_ENTRYPOINT_COUNT
    assert len(SCRIPT_ENTRYPOINTS) == SCRIPT_ENTRYPOINT_COUNT

    for name, mode in (
        *((name, "python") for name in PYTHON_ENTRYPOINTS),
        *((name, "script") for name in SCRIPT_ENTRYPOINTS),
    ):
        text = (REPO_ROOT / "scripts" / name).read_text()
        assert text.startswith("#!/usr/bin/env python3\n")
        assert "from scripts.lib.uv_bootstrap import bootstrap_uv" in text
        assert f'_bootstrap_uv(script_path=__file__, mode="{mode}")' in text


@pytest.mark.parametrize(
    ("mode", "expected_command"),
    [
        ("python", ("--project", "python")),
        ("script", ("--script",)),
    ],
)
def test_bootstrap_reexecs_with_the_matching_uv_run_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: uv_bootstrap.RunMode,
    expected_command: tuple[str, ...],
) -> None:
    script_path = tmp_path / "scripts" / "entrypoint.py"
    script_path.parent.mkdir()
    script_path.touch()
    candidate = uv_resolve.UvCandidate("/fake/compatible/uv", (0, 11, 26), "")
    monkeypatch.delenv(uv_bootstrap.UV_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(uv_bootstrap.sys, "prefix", "/system-python")
    monkeypatch.setattr(uv_bootstrap.sys, "base_prefix", "/system-python")
    assert uv_bootstrap.sys.prefix == uv_bootstrap.sys.base_prefix
    assert uv_bootstrap.UV_BOOTSTRAP_ENV not in os.environ

    def resolve(**_: object) -> uv_resolve.UvCandidate:
        return candidate

    monkeypatch.setattr(
        uv_bootstrap,
        "find_compatible_uv_for_repo",
        resolve,
    )
    monkeypatch.setattr(uv_bootstrap.sys, "argv", [str(script_path), "--flag"])
    chdir = MagicMock()
    execv = MagicMock(side_effect=SystemExit(0))
    monkeypatch.setattr(uv_bootstrap.os, "chdir", chdir)
    monkeypatch.setattr(uv_bootstrap.os, "execv", execv)

    with pytest.raises(SystemExit):
        uv_bootstrap.bootstrap_uv(script_path=str(script_path), mode=mode)

    chdir.assert_called_once_with(tmp_path)
    execv.assert_called_once_with(
        "/fake/compatible/uv",
        [
            "/fake/compatible/uv",
            "run",
            *expected_command[:1],
            *((str(tmp_path),) if mode == "python" else ()),
            *expected_command[1:],
            str(script_path.resolve()),
            "--flag",
        ],
    )
    assert os.environ[uv_bootstrap.UV_BOOTSTRAP_ENV] == str(script_path.resolve())


def test_bootstrap_skips_when_sentinel_matches_the_current_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_path = REPO_ROOT / "scripts" / "docs-pages-lint.py"
    monkeypatch.setenv(uv_bootstrap.UV_BOOTSTRAP_ENV, str(script_path.resolve()))
    monkeypatch.setattr(uv_bootstrap.sys, "prefix", "/system-python")
    monkeypatch.setattr(uv_bootstrap.sys, "base_prefix", "/system-python")
    resolve = MagicMock(side_effect=AssertionError("must not resolve uv"))
    execv = MagicMock(side_effect=AssertionError("must not exec"))
    monkeypatch.setattr(uv_bootstrap, "find_compatible_uv_for_repo", resolve)
    monkeypatch.setattr(uv_bootstrap.os, "execv", execv)

    uv_bootstrap.bootstrap_uv(script_path=str(script_path), mode="script")

    resolve.assert_not_called()
    execv.assert_not_called()


@pytest.mark.parametrize("mode", ["python", "script"])
def test_bootstrap_reexecs_when_sentinel_belongs_to_another_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: uv_bootstrap.RunMode,
) -> None:
    script_path = tmp_path / "scripts" / "child.py"
    script_path.parent.mkdir()
    script_path.touch()
    parent_script = tmp_path / "scripts" / "parent.py"
    candidate = uv_resolve.UvCandidate("/fake/compatible/uv", (0, 11, 26), "")
    monkeypatch.setenv(uv_bootstrap.UV_BOOTSTRAP_ENV, str(parent_script))
    monkeypatch.setattr(uv_bootstrap.sys, "prefix", "/system-python")
    monkeypatch.setattr(uv_bootstrap.sys, "base_prefix", "/system-python")
    resolve = MagicMock(return_value=candidate)
    monkeypatch.setattr(
        uv_bootstrap,
        "find_compatible_uv_for_repo",
        resolve,
    )
    monkeypatch.setattr(uv_bootstrap.sys, "argv", [str(script_path)])
    monkeypatch.setattr(uv_bootstrap.os, "chdir", MagicMock())
    execv = MagicMock(side_effect=SystemExit(0))
    monkeypatch.setattr(uv_bootstrap.os, "execv", execv)

    with pytest.raises(SystemExit):
        uv_bootstrap.bootstrap_uv(script_path=str(script_path), mode=mode)

    resolve.assert_called_once_with(cwd=str(tmp_path))
    execv.assert_called_once_with(
        candidate.path,
        [
            candidate.path,
            "run",
            *(
                ("--project", str(tmp_path), "python")
                if mode == "python"
                else ("--script",)
            ),
            str(script_path.resolve()),
        ],
    )
    assert os.environ[uv_bootstrap.UV_BOOTSTRAP_ENV] == str(script_path.resolve())


def test_bootstrap_skips_an_active_virtualenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(uv_bootstrap.UV_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(uv_bootstrap.sys, "prefix", "/active-venv")
    monkeypatch.setattr(uv_bootstrap.sys, "base_prefix", "/base-python")
    execv = MagicMock(side_effect=AssertionError("must not exec"))
    monkeypatch.setattr(uv_bootstrap.os, "execv", execv)

    uv_bootstrap.bootstrap_uv(
        script_path=str(REPO_ROOT / "scripts" / "docs-pages-lint.py"),
        mode="script",
    )

    execv.assert_not_called()
