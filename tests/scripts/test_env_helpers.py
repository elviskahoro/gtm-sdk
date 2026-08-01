"""Unit tests for the shared bootstrap helpers in scripts/lib/env.py.

These helpers are on the critical path for every repo script that
self-bootstraps `infisical run` (e.g. attio-meeting_relationship-inspect,
attio-workspace_slug-probe), so a regression in `parse_dotenv` or credential
resolution would silently break bootstrap across multiple commands. BD: ai-3hq.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts.lib import env


def test_clean_env_strips_and_blanks_to_none() -> None:
    assert env.clean_env("  abc \n") == "abc"
    assert env.clean_env("   ") is None
    assert env.clean_env("") is None
    assert env.clean_env(None) is None


def test_parse_dotenv_handles_export_quotes_and_comments() -> None:
    text = "\n".join(
        [
            "# a comment",
            "",
            "export INFISICAL_PROJECT_ID=proj-123",
            'INFISICAL_TOKEN="st.tok.en"',
            "SINGLE='quoted value'",
            "UNQUOTED=bare # trailing comment",
            "NO_EQUALS_LINE",
        ],
    )
    parsed = env.parse_dotenv(text)
    assert parsed["INFISICAL_PROJECT_ID"] == "proj-123"
    assert parsed["INFISICAL_TOKEN"] == "st.tok.en"
    # Quoted values are kept verbatim; a `#` inside is not a comment.
    assert parsed["SINGLE"] == "quoted value"
    # Unquoted values strip an inline ` # comment`.
    assert parsed["UNQUOTED"] == "bare"
    assert "NO_EQUALS_LINE" not in parsed


def test_parse_dotenv_keeps_hash_inside_quoted_value() -> None:
    parsed = env.parse_dotenv('TOKEN="abc#def"')
    assert parsed["TOKEN"] == "abc#def"


def test_parse_dotenv_strips_comment_after_quoted_value() -> None:
    # `KEY="val" # comment` must yield `val`, not `"val"` (quotes intact would
    # break Infisical auth). The closing quote bounds the value; the rest drops.
    parsed = env.parse_dotenv('INFISICAL_TOKEN="st.tok.en" # generated 2026')
    assert parsed["INFISICAL_TOKEN"] == "st.tok.en"


def test_parse_dotenv_unterminated_quote_keeps_remainder() -> None:
    parsed = env.parse_dotenv('KEY="oops')
    assert parsed["KEY"] == "oops"


def test_read_infisical_credentials_prefers_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "env-proj")
    monkeypatch.setenv("INFISICAL_TOKEN", "env-tok")
    assert env.read_infisical_credentials() == ("env-proj", "env-tok")


def test_read_infisical_credentials_falls_back_to_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("INFISICAL_PROJECT_ID", raising=False)
    monkeypatch.delenv("INFISICAL_TOKEN", raising=False)
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        'INFISICAL_PROJECT_ID="file-proj"\nINFISICAL_TOKEN=file-tok\n',
    )
    monkeypatch.setattr(env, "REPO_ROOT", tmp_path)
    assert env.read_infisical_credentials() == ("file-proj", "file-tok")


def test_read_infisical_credentials_does_not_mix_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Only the project id is in the environment; the token is only in the file.
    # The pair must come wholly from the file — never one value from each source.
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "env-proj")
    monkeypatch.delenv("INFISICAL_TOKEN", raising=False)
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "INFISICAL_PROJECT_ID=file-proj\nINFISICAL_TOKEN=file-tok\n",
    )
    monkeypatch.setattr(env, "REPO_ROOT", tmp_path)
    assert env.read_infisical_credentials() == ("file-proj", "file-tok")


def test_read_infisical_credentials_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("INFISICAL_PROJECT_ID", raising=False)
    monkeypatch.delenv("INFISICAL_TOKEN", raising=False)
    monkeypatch.setattr(env, "REPO_ROOT", tmp_path)  # empty dir, no .env.local
    assert env.read_infisical_credentials() is None


def test_infisical_run_example_includes_required_flags() -> None:
    example = env.infisical_run_example("scripts/foo.py")
    assert "--projectId" in example
    assert "--token" in example
    assert "--env=" in example
    assert "scripts/foo.py" in example


# --- Execution backend selection -------------------------------------------
#
# `GTM_EXEC_BACKEND` replaced `DAGGER_DRY_RUN`, which was misnamed (it never
# implied a dry run -- it really deployed) and conflated two different jobs:
# the stub-binary test path and the documented Conductor-sandbox path. These
# tests pin the split, and pin that the `flox` backend never degrades to bare
# PATH -- silent degradation would report success while handing the operator
# the opposite of the pinned toolchain they asked for.


def _which_found(_name: str) -> str:
    return "/usr/bin/flox"


def _which_missing(_name: str) -> None:
    return None


def _activation_ok(
    *_args: object,
    **_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _preflight_noop(_required_tools: Sequence[str] = ()) -> Path:
    return Path("/unused")


def test_resolve_exec_backend_defaults_to_dagger() -> None:
    """Explicit empty mapping, not the ambient env — keeps this hermetic."""
    assert env.resolve_exec_backend({}) is env.ExecBackend.DAGGER


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("dagger", env.ExecBackend.DAGGER),
        ("flox", env.ExecBackend.FLOX),
        ("host", env.ExecBackend.HOST),
        ("FLOX", env.ExecBackend.FLOX),
        ("  host  ", env.ExecBackend.HOST),
    ],
)
def test_resolve_exec_backend_reads_explicit_value(
    raw: str,
    expected: env.ExecBackend,
) -> None:
    assert env.resolve_exec_backend({env.EXEC_BACKEND_ENV: raw}) is expected


def test_resolve_exec_backend_rejects_unknown_value() -> None:
    """A typo must not quietly become the default."""
    with pytest.raises(ValueError, match="is not one of"):
        env.resolve_exec_backend({env.EXEC_BACKEND_ENV: "flux"})


def test_legacy_dry_run_maps_to_host_and_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        env.resolve_exec_backend({env.LEGACY_DRY_RUN_ENV: "1"}) is env.ExecBackend.HOST
    )
    stderr = capsys.readouterr().err
    assert env.LEGACY_DRY_RUN_ENV in stderr
    assert "deprecated" in stderr
    # The warning has to name the replacement for both jobs, or an operator
    # migrating off it lands on `host` and silently loses the pinning.
    assert f"{env.EXEC_BACKEND_ENV}=host" in stderr
    assert f"{env.EXEC_BACKEND_ENV}=flox" in stderr


@pytest.mark.parametrize("raw", ["0", "", "true", "yes"])
def test_legacy_dry_run_only_triggers_on_exactly_one(raw: str) -> None:
    """Mirrors the original `== "1"` check; anything else stays on Dagger."""
    assert (
        env.resolve_exec_backend({env.LEGACY_DRY_RUN_ENV: raw})
        is env.ExecBackend.DAGGER
    )


def test_explicit_backend_wins_over_legacy_alias() -> None:
    resolved = env.resolve_exec_backend(
        {env.EXEC_BACKEND_ENV: "dagger", env.LEGACY_DRY_RUN_ENV: "1"},
    )
    assert resolved is env.ExecBackend.DAGGER


def test_flox_activate_prefix_is_run_mode_and_repo_scoped() -> None:
    """`--mode run` is load-bearing: dev mode refuses while an agent holds one."""
    prefix = env.flox_activate_prefix()
    assert prefix[:2] == ["flox", "activate"]
    assert prefix[-1] == "--"
    assert "--mode" in prefix
    assert prefix[prefix.index("--mode") + 1] == "run"
    assert prefix[prefix.index("--dir") + 1] == str(env.REPO_ROOT)


@pytest.mark.parametrize(
    ("machine", "system", "expected_arch"),
    [
        ("x86_64", "Linux", "x86_64"),
        ("aarch64", "Linux", "aarch64"),
        # Flox names the dir aarch64 on Apple silicon, where platform.machine()
        # reports arm64 -- the Python twin of the setup script's sed.
        ("arm64", "Darwin", "aarch64"),
    ],
)
def test_flox_bin_dir_matches_setup_script_derivation(
    monkeypatch: pytest.MonkeyPatch,
    machine: str,
    system: str,
    expected_arch: str,
) -> None:
    monkeypatch.setattr(env.platform, "machine", lambda: machine)
    monkeypatch.setattr(env.platform, "system", lambda: system)
    bin_dir = env.flox_bin_dir()
    assert bin_dir.parent.name == f"{expected_arch}-{system.lower()}.gtm-sdk-run"
    assert bin_dir.name == "bin"


def test_wrap_for_backend_leaves_host_argv_untouched() -> None:
    argv = ["infisical", "run", "--", "uv", "run", "modal", "deploy", "x.py"]
    assert env.wrap_for_backend(argv, env.ExecBackend.HOST) == argv


def test_wrap_for_backend_prefixes_flox_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(env, "preflight_flox", _preflight_noop)
    argv = ["infisical", "run"]
    wrapped = env.wrap_for_backend(argv, env.ExecBackend.FLOX)
    assert wrapped == [*env.flox_activate_prefix(), *argv]


def test_preflight_flox_raises_when_flox_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(env.shutil, "which", _which_missing)
    with pytest.raises(env.FloxBackendError, match="not on PATH") as excinfo:
        env.preflight_flox()
    # Actionable or it is just a different silent failure.
    assert "conductor-workspace-setup.sh" in str(excinfo.value)


def test_preflight_flox_raises_when_activation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(env.shutil, "which", _which_found)

    def _failed_activate(
        *_args: object,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="nix-daemon unreachable",
        )

    monkeypatch.setattr(env.subprocess, "run", _failed_activate)
    with pytest.raises(env.FloxBackendError, match="nix-daemon unreachable"):
        env.preflight_flox()


def test_preflight_flox_raises_when_env_realized_but_bin_dir_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The exact state observed on a live sandbox: `.flox/run/` left empty."""
    monkeypatch.setattr(env.shutil, "which", _which_found)
    monkeypatch.setattr(env.subprocess, "run", _activation_ok)
    monkeypatch.setattr(env, "flox_bin_dir", lambda: tmp_path / "absent" / "bin")
    with pytest.raises(env.FloxBackendError, match="is absent"):
        env.preflight_flox()


def test_preflight_flox_raises_when_a_required_tool_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").touch()
    monkeypatch.setattr(env.shutil, "which", _which_found)
    monkeypatch.setattr(env.subprocess, "run", _activation_ok)
    monkeypatch.setattr(env, "flox_bin_dir", lambda: bin_dir)

    assert env.preflight_flox(("uv",)) == bin_dir
    with pytest.raises(env.FloxBackendError, match="infisical"):
        env.preflight_flox(("uv", "infisical"))
