"""Regression tests for Conductor workspace provisioning.

Each test runs a copy of the setup script in an isolated temporary repository.
The fake tools model only the contracts used by setup, so no test downloads,
installs, or modifies a developer's real global Git configuration.
"""
# ruff: noqa: S101

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "scripts" / "conductor-workspace-setup.sh"
UV_RESOLVE_MODULE = REPO_ROOT / "scripts" / "lib" / "uv_resolve.py"
CONDUCTOR_SETTINGS = REPO_ROOT / ".conductor" / "settings.toml"

# Matches the setup script's own UV_PINNED_VERSION literal, and what the
# fake pinned-installer stub below drops onto disk when "installed".
_UV_PINNED_VERSION = "0.11.26"


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_stub(bin_dir: Path, name: str, contents: str) -> None:
    path = bin_dir / name
    path.write_text(textwrap.dedent(contents))
    _make_executable(path)


def _write_uv_stub(bin_dir: Path, *, version: str = _UV_PINNED_VERSION) -> None:
    """A `uv` stub that answers `--version` for real, plus the generic log line.

    Once `conductor-workspace-setup.sh`'s fallback block does real
    compatibility checking (via `scripts/lib/uv_resolve.py`), a stub that
    doesn't answer `--version` meaningfully would look "unparseable" and
    trigger a spurious reinstall in every test that doesn't care about uv
    version behavior specifically. Defaults to a compatible version so those
    tests are unaffected.
    """
    _write_stub(
        bin_dir,
        "uv",
        f"""\
        #!/usr/bin/env bash
        echo "fallback-uv $*" >> "${{SETUP_TEST_LOG}}"
        if [[ "${{1:-}}" == "--version" ]]; then
          echo "uv {version} (stub)"
          exit 0
        fi
        exit 0
        """,
    )


def _write_common_stubs(bin_dir: Path, *, kernel: str = "Linux") -> None:
    _write_stub(
        bin_dir,
        "git",
        """\
        #!/usr/bin/env bash
        if [[ "${1:-}" == "rev-parse" && "${2:-}" == "--git-common-dir" ]]; then
          printf '%s\\n' "${SETUP_TEST_GIT_COMMON_DIR:-${PWD}/.git}"
          exit 0
        fi
        case "${1:-}" in
          rev-parse) printf '%s\n' "${PWD}/.git" ;;
          config) printf '%s\n' "$*" >> "${SETUP_TEST_LOG}" ;;
          submodule) ;;
          *) exit 1 ;;
        esac
        """,
    )
    _write_stub(
        bin_dir,
        "uname",
        f"""\
        #!/usr/bin/env bash
        if [[ "${{1:-}}" == "-s" ]]; then
          echo {kernel}
        else
          echo x86_64
        fi
        """,
    )
    for tool in ("dolt", "infisical", "gh"):
        _write_stub(
            bin_dir,
            tool,
            f"""\
            #!/usr/bin/env bash
            echo "fallback-{tool} $*" >> "${{SETUP_TEST_LOG}}"
            """,
        )


def _write_flox(bin_dir: Path, flox_bin: Path, *, succeeds: bool) -> None:
    exit_code = 0 if succeeds else 1
    _write_stub(
        bin_dir,
        "flox",
        f"""\
        #!/usr/bin/env bash
        echo "flox $*" >> "${{SETUP_TEST_LOG}}"
        [[ "${{1:-}}" == "activate" ]] || exit 1
        exit {exit_code}
        """,
    )
    if succeeds:
        flox_bin.mkdir(parents=True)
        for tool in ("uv", "dolt", "infisical", "gh", "bd", "roborev"):
            _write_stub(
                flox_bin,
                tool,
                f"""\
                #!/usr/bin/env bash
                echo "flox-{tool} $*" >> "${{SETUP_TEST_LOG}}"
                """,
            )


def _write_curl_installer(
    bin_dir: Path,
    *,
    installed_uv_version: str = _UV_PINNED_VERSION,
    failing_tool: str | None = None,
) -> None:
    """Fake `curl | sh`-style installers.

    Each match writes the "installed" stub binary directly as a side effect
    (rather than trying to fake a piped-through installer script's own
    logic) and prints a no-op `:` as the curl's stdout, which the real
    invocation pipes into `sh`/`bash` and does nothing further with.

    The uv branch ignores the actual pinned-version path segment in the
    matched URL and always drops a stub reporting `installed_uv_version` --
    good enough to prove "the pinned-install path ran and produced a usable
    compatible uv" without parsing version numbers out of URLs in bash.

    `failing_tool` (one of "bd"/"roborev"/"uv") makes that tool's installer
    exit non-zero without installing anything -- models a flaky upstream
    download (e.g. a transient GitHub-releases 500) to prove setup survives
    it instead of dying under `set -e`.
    """
    fail_case = ""
    if failing_tool is not None:
        fail_case = f"""
        if [[ "${{tool}}" == "{failing_tool}" ]]; then
          exit 1
        fi
        """
    _write_stub(
        bin_dir,
        "curl",
        f"""\
        #!/usr/bin/env bash
        case "$*" in
          *gastownhall/beads*)
            tool=bd
            ;;
          *roborev.io*)
            tool=roborev
            ;;
          *astral-sh/uv/releases/download/*uv-installer.sh*)
            tool=uv
            ;;
          *)
            exit 1
            ;;
        esac
        {fail_case}
        target="${{HOME}}/.local/bin/${{tool}}"
        mkdir -p "${{HOME}}/.local/bin"
        if [[ "${{tool}}" == "uv" ]]; then
          printf '#!/usr/bin/env bash\\necho "pinned-uv-install $*" >> "%s"\\nif [[ "$1" == "--version" ]]; then echo "uv {installed_uv_version} (pinned-stub)"; exit 0; fi\\nexit 0\\n' \\
            "${{SETUP_TEST_LOG}}" > "${{target}}"
        else
          printf '#!/usr/bin/env bash\\necho "fallback-%s $1" >> "%s"\\n' \\
            "${{tool}}" "${{SETUP_TEST_LOG}}" > "${{target}}"
        fi
        chmod +x "${{target}}"
        printf ':\\n'
        """,
    )


def _run_setup(
    tmp_path: Path,
    *,
    flox_succeeds: bool,
    kernel: str = "Linux",
    git_common_dir: Path | None = None,
    stale_beads_target: Path | None = None,
    uv_version: str | None = _UV_PINNED_VERSION,
    later_uv_version: str | None = None,
    failing_curl_tool: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True)
    lib_dir = scripts_dir / "lib"
    lib_dir.mkdir()
    # scripts/conductor-workspace-setup.sh now shells out to
    # scripts/lib/uv_resolve.py -- copy the real module in alongside the
    # copied setup script, same as any other tracked file a fresh worktree
    # would have. uv_resolve.py reads pyproject.toml's own required-version
    # relative to *its own* file location, so the fixture needs a real copy
    # of that too.
    (lib_dir / UV_RESOLVE_MODULE.name).write_text(UV_RESOLVE_MODULE.read_text())
    (repo / "pyproject.toml").write_text((REPO_ROOT / "pyproject.toml").read_text())
    flox_bin = repo / ".flox" / "run" / "x86_64-linux.gtm-sdk-run" / "bin"
    (repo / ".git").mkdir()
    setup_copy = scripts_dir / SETUP_SCRIPT.name
    setup_copy.write_text(SETUP_SCRIPT.read_text())
    _make_executable(setup_copy)
    if stale_beads_target is not None:
        (repo / ".beads").symlink_to(stale_beads_target)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_common_stubs(bin_dir, kernel=kernel)
    if uv_version is not None:
        _write_uv_stub(bin_dir, version=uv_version)
    _write_flox(bin_dir, flox_bin, succeeds=flox_succeeds)
    _write_curl_installer(bin_dir, failing_tool=failing_curl_tool)

    # Optional second `uv`-only bin dir, placed *later* on PATH than
    # `bin_dir` -- simulates a compatible install (e.g. Homebrew) sitting
    # behind an earlier, incompatible one (e.g. a pyenv shim), the exact
    # PATH-order shape from the original bug report.
    later_uv_dir = None
    if later_uv_version is not None:
        later_uv_dir = tmp_path / "bin-later-uv"
        later_uv_dir.mkdir()
        _write_uv_stub(later_uv_dir, version=later_uv_version)

    log = tmp_path / "setup.log"
    home = tmp_path / "home"
    home.mkdir()
    path_parts = [str(bin_dir)]
    if later_uv_dir is not None:
        path_parts.append(str(later_uv_dir))
    path_parts += [str(Path(sys.executable).parent), "/usr/bin", "/bin"]
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join(path_parts),
        "SETUP_TEST_LOG": str(log),
    }
    if git_common_dir is not None:
        env["SETUP_TEST_GIT_COMMON_DIR"] = str(git_common_dir)
    if extra_env is not None:
        env.update(extra_env)
    return (
        subprocess.run(
            ["bash", str(setup_copy)],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        ),
        log,
    )


def test_successful_flox_activation_uses_flox_provided_tools(tmp_path: Path) -> None:
    result, log = _run_setup(tmp_path, flox_succeeds=True)

    assert result.returncode == 0, result.stderr
    assert "provisioning source: Flox" in result.stdout
    assert "flox activate" in log.read_text()
    assert "flox-bd version" in log.read_text()
    assert "flox-roborev version" in log.read_text()
    assert "fallback-roborev" not in log.read_text()


def test_failed_flox_activation_uses_fallback_installers(tmp_path: Path) -> None:
    result, log = _run_setup(tmp_path, flox_succeeds=False)

    assert result.returncode == 0, result.stderr
    assert (
        "warning: Flox activation or materialization failed; "
        "using fallback installers" in result.stdout
    )
    assert "provisioning source: fallback installers" in result.stdout
    assert "info: installing roborev with fallback installer" in result.stdout
    assert "fallback-bd version" in log.read_text()
    assert "fallback-roborev version" in log.read_text()
    assert "config --global alias.roborev !roborev" in log.read_text()


def test_conductor_shells_disable_zsh_compfix() -> None:
    settings = tomllib.loads(CONDUCTOR_SETTINGS.read_text())

    assert settings["environment_variables"]["ZSH_DISABLE_COMPFIX"] == "true"


def test_workspace_setup_does_not_initialize_zsh_completion() -> None:
    setup_script = SETUP_SCRIPT.read_text().lower()

    assert "compinit" not in setup_script
    assert "compaudit" not in setup_script


def test_worktree_links_beads_to_primary_checkout(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    (primary / ".git").mkdir(parents=True)
    (primary / ".beads").mkdir()

    result, _ = _run_setup(
        tmp_path,
        flox_succeeds=True,
        git_common_dir=primary / ".git",
    )

    assert result.returncode == 0, result.stderr
    workspace_beads = tmp_path / "repo" / ".beads"
    assert workspace_beads.is_symlink()
    assert workspace_beads.resolve() == (primary / ".beads").resolve()


def test_worktree_repairs_stale_beads_symlink(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    (primary / ".git").mkdir(parents=True)
    (primary / ".beads").mkdir()
    stale = tmp_path / "wrong" / ".beads"
    stale.parent.mkdir()
    stale.mkdir()

    result, _ = _run_setup(
        tmp_path,
        flox_succeeds=True,
        git_common_dir=primary / ".git",
        stale_beads_target=stale,
    )

    assert result.returncode == 0, result.stderr
    workspace_beads = tmp_path / "repo" / ".beads"
    assert workspace_beads.resolve() == (primary / ".beads").resolve()


def test_primary_checkout_preserves_its_existing_beads_symlink(tmp_path: Path) -> None:
    primary = tmp_path / "repo"
    shared_beads = tmp_path / "shared-beads"
    shared_beads.mkdir()

    result, _ = _run_setup(
        tmp_path,
        flox_succeeds=True,
        git_common_dir=primary / ".git",
        stale_beads_target=shared_beads,
    )

    assert result.returncode == 0, result.stderr
    assert (primary / ".beads").is_symlink()
    assert (primary / ".beads").resolve() == shared_beads.resolve()


def test_primary_checkout_removes_dangling_beads_symlink(tmp_path: Path) -> None:
    primary = tmp_path / "repo"
    missing_beads = tmp_path / "missing-beads"

    result, _ = _run_setup(
        tmp_path,
        flox_succeeds=True,
        git_common_dir=primary / ".git",
        stale_beads_target=missing_beads,
    )

    assert result.returncode == 0, result.stderr
    assert not (primary / ".beads").is_symlink()


# ---------------------------------------------------------------------------
# uv version-compatibility fallback (Darwin / non-Flox path)
#
# Previously this branch only checked `command -v uv` (presence), so an
# already-resolvable but *incompatible* uv (e.g. a stray pyenv shim) would
# skip installation entirely -- see scripts/lib/uv_resolve.py and
# AGENTS.md "Scripted deploy pitfalls" for the full story. None of the tests
# above exercise a Darwin kernel or a pre-existing incompatible uv at all.
# ---------------------------------------------------------------------------


def test_macos_fallback_leaves_existing_compatible_uv_alone(tmp_path: Path) -> None:
    result, log = _run_setup(
        tmp_path,
        flox_succeeds=False,
        kernel="Darwin",
        uv_version=_UV_PINNED_VERSION,
    )

    assert result.returncode == 0, result.stderr
    assert "provisioning source: fallback installers" in result.stdout
    assert "using compatible uv at" in result.stdout
    assert "pinned-uv-install" not in log.read_text(), (
        "a compatible uv was already on PATH; nothing should have been reinstalled"
    )


def test_macos_fallback_installs_pinned_uv_when_existing_one_is_incompatible(
    tmp_path: Path,
) -> None:
    result, log = _run_setup(
        tmp_path,
        flox_succeeds=False,
        kernel="Darwin",
        uv_version="0.11.7",  # the exact incompatible version from the bug report
    )

    assert result.returncode == 0, result.stderr
    assert (
        "no uv on PATH satisfies required-version; installing pinned uv"
        in result.stdout
    )
    assert "pinned-uv-install" in log.read_text()
    assert "using compatible uv at" in result.stdout


def test_macos_fallback_skips_reinstall_when_a_later_path_entry_is_compatible(
    tmp_path: Path,
) -> None:
    """An incompatible `uv` earlier on PATH must not force a reinstall.

    Regression target for the exact reported bug shape: a pyenv shim
    (incompatible) ahead of a Homebrew install (compatible) on PATH. The
    old presence-only check would have stopped at the first `uv` found,
    even an incompatible one; the fix must resolve past it to the later,
    compatible entry -- without installing anything new.
    """
    result, log = _run_setup(
        tmp_path,
        flox_succeeds=False,
        kernel="Darwin",
        uv_version="0.11.7",  # earlier on PATH, incompatible
        later_uv_version=_UV_PINNED_VERSION,  # later on PATH, compatible
    )

    assert result.returncode == 0, result.stderr
    assert "using compatible uv at" in result.stdout
    log_text = log.read_text()
    assert "pinned-uv-install" not in log_text, (
        "a compatible uv existed later on PATH; nothing should have been installed"
    )


# ---------------------------------------------------------------------------
# Beads DB bootstrap (DoltHub seeding)
#
# A flaky fallback installer (roborev/bd) used to be able to kill the whole
# `set -e` script before it ever reached the Beads bootstrap block below it,
# and a `DOLTHUB_API_KEY` that simply never resolved fell back to an empty
# local database with no log output at all. Both are regression targets.
# ---------------------------------------------------------------------------


def test_failed_roborev_install_does_not_abort_setup(tmp_path: Path) -> None:
    result, _log = _run_setup(
        tmp_path,
        flox_succeeds=False,
        kernel="Darwin",
        failing_curl_tool="roborev",
    )

    assert result.returncode == 0, result.stderr
    assert "warning: roborev fallback install failed, continuing without roborev" in (
        result.stdout
    )
    # Setup must still reach the Beads bootstrap block after the failure.
    assert (
        "warning: DOLTHUB_API_KEY not available from .env.local or Infisical"
        in result.stdout
    )


def test_failed_bd_install_reports_a_clear_warning_before_bd_is_needed(
    tmp_path: Path,
) -> None:
    """A failed `bd` install can't be papered over -- bootstrap genuinely needs `bd`.

    Unlike roborev, nothing downstream can substitute for a missing `bd`, so
    this case still ends in failure. What changed is *where* and *how*: before
    this fix, the unguarded `curl | bash` pipeline died under `set -e` right
    there with no diagnostic; now the install failure is reported explicitly,
    and the script proceeds into the Beads bootstrap block (reaching the
    DOLTHUB_API_KEY warning) before failing later for the obvious reason
    (`bd: command not found`) instead of an opaque curl/pipe abort.
    """
    result, _log = _run_setup(
        tmp_path,
        flox_succeeds=False,
        kernel="Darwin",
        failing_curl_tool="bd",
    )

    assert "warning: bd fallback install failed, continuing without bd" in result.stdout
    assert (
        "warning: DOLTHUB_API_KEY not available from .env.local or Infisical"
        in result.stdout
    )
    assert "bd: command not found" in result.stderr


def test_missing_dolthub_api_key_reports_explicit_warning(tmp_path: Path) -> None:
    result, _log = _run_setup(tmp_path, flox_succeeds=False, kernel="Darwin")

    assert result.returncode == 0, result.stderr
    assert (
        "warning: DOLTHUB_API_KEY not available from .env.local or Infisical; "
        "falling back to a fresh local Beads database instead of "
        "https://doltremoteapi.dolthub.com/elviskahoro/gtm-sdk" in result.stdout
    )


def test_available_dolthub_api_key_skips_missing_key_warning(tmp_path: Path) -> None:
    result, _log = _run_setup(
        tmp_path,
        flox_succeeds=False,
        kernel="Darwin",
        extra_env={"DOLTHUB_API_KEY": "test-token"},
    )

    assert result.returncode == 0, result.stderr
    assert "warning: DOLTHUB_API_KEY not available" not in result.stdout
