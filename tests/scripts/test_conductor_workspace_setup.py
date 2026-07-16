"""Regression tests for Conductor workspace provisioning.

Each test runs a copy of the setup script in an isolated temporary repository.
The fake tools model only the contracts used by setup, so no test downloads,
installs, or modifies a developer's real global Git configuration.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "scripts" / "conductor-workspace-setup.sh"
CONDUCTOR_SETTINGS = REPO_ROOT / ".conductor" / "settings.toml"


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_stub(bin_dir: Path, name: str, contents: str) -> None:
    path = bin_dir / name
    path.write_text(textwrap.dedent(contents))
    _make_executable(path)


def _write_common_stubs(bin_dir: Path) -> None:
    _write_stub(
        bin_dir,
        "git",
        """\
        #!/usr/bin/env bash
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
        """\
        #!/usr/bin/env bash
        if [[ "${1:-}" == "-s" ]]; then
          echo Linux
        else
          echo x86_64
        fi
        """,
    )
    for tool in ("dolt", "uv", "infisical", "gh"):
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


def _write_curl_installer(bin_dir: Path) -> None:
    _write_stub(
        bin_dir,
        "curl",
        """\
        #!/usr/bin/env bash
        case "$*" in
          *gastownhall/beads*)
            tool=bd
            ;;
          *roborev.io*)
            tool=roborev
            ;;
          *)
            exit 1
            ;;
        esac
        target="${HOME}/.local/bin/${tool}"
        mkdir -p "${HOME}/.local/bin"
        printf '#!/usr/bin/env bash\necho "fallback-%s $1" >> "%s"\n' \
          "${tool}" "${SETUP_TEST_LOG}" > "${target}"
        chmod +x "${target}"
        printf ':\n'
        """,
    )


def _run_setup(
    tmp_path: Path, *, flox_succeeds: bool
) -> tuple[subprocess.CompletedProcess[str], Path]:
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True)
    flox_bin = repo / ".flox" / "run" / "x86_64-linux.gtm-sdk-run" / "bin"
    (repo / ".git").mkdir()
    setup_copy = scripts_dir / SETUP_SCRIPT.name
    setup_copy.write_text(SETUP_SCRIPT.read_text())
    _make_executable(setup_copy)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_common_stubs(bin_dir)
    _write_flox(bin_dir, flox_bin, succeeds=flox_succeeds)
    _write_curl_installer(bin_dir)

    log = tmp_path / "setup.log"
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "SETUP_TEST_LOG": str(log),
    }
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
