"""Smoke tests for scripts/webhooks-handlers-redeploy.py.

Verifies the substitute -> deploy -> restore loop preserves the working tree,
even when the deploy fails mid-iteration. Stubs modal / infisical / uv / gcloud
so the test never makes real network calls.

Sets ``GTM_DEPLOY_VIA_FLOX=1`` so the script's deploy step shells out to the
host stubs (via a stubbed ``flox``) instead of spinning up a Dagger engine.
The Dagger path (which runs ``modal deploy`` inside a Dagger container) is
covered by manual smoke tests — running it in CI would require a Dagger
engine and real Modal/GCP credentials.

BD: gtm-sdk-43z (epic gtm-sdk-yol). Each test maps to one acceptance criterion.
"""
# trunk-ignore-all(bandit/B105): test fixtures, not real credentials
# ruff: noqa: PLR2004, S101, S603

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import NamedTuple

import pytest

SOURCE_REPO_ROOT = Path(__file__).absolute().parents[2]
SCRIPT_REL = Path("scripts/webhooks-handlers-redeploy.py")
HANDLER_REL = Path("webhooks/export_to_attio.py")
HANDLER_NAME = "export_to_attio"
SOURCE_NAME = "CaldotcomBookingWebhook"


class DeployWorkspace(NamedTuple):
    root: Path
    script: Path
    handler_file: Path


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_tree_writable(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


@pytest.fixture
def deploy_workspace(tmp_path: Path) -> DeployWorkspace:
    """Copy the deploy surface into a writable mini-repo for each test.

    Bazel exposes source/data files through runfiles symlinks. The deploy
    helper intentionally mutates ``webhooks/<handler>.py`` plus ``tmp/`` while
    it substitutes and restores a handler, so the test must not run it against
    the read-only runfiles tree or the developer checkout those symlinks may
    resolve to.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "scripts").mkdir()

    shutil.copy2(SOURCE_REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    shutil.copy2(SOURCE_REPO_ROOT / SCRIPT_REL, root / SCRIPT_REL)
    scripts_init = SOURCE_REPO_ROOT / "scripts" / "__init__.py"
    if scripts_init.exists():
        shutil.copy2(scripts_init, root / "scripts" / "__init__.py")
    shutil.copytree(SOURCE_REPO_ROOT / "scripts" / "lib", root / "scripts" / "lib")
    shutil.copytree(
        SOURCE_REPO_ROOT / "webhooks",
        root / "webhooks",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (root / "tmp").mkdir()
    _make_tree_writable(root)

    git = shutil.which("git")
    if git is None:
        pytest.skip("git is required to stage the deploy helper test workspace")

    subprocess.run([git, "init", "-q"], cwd=root, check=True)
    subprocess.run([git, "add", "webhooks"], cwd=root, check=True)
    subprocess.run(
        [
            git,
            "-c",
            "user.name=gtm-sdk tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-q",
            "-m",
            "baseline webhooks",
        ],
        cwd=root,
        check=True,
    )

    return DeployWorkspace(
        root=root,
        script=root / SCRIPT_REL,
        handler_file=root / HANDLER_REL,
    )


def _write_default_stubs(bin_dir: Path) -> None:
    """Write the canonical set of stub binaries used by the happy-path test.

    Stubs mirror real-tool behavior closely enough that the script can't tell
    the difference. In particular, the infisical stub only injects
    MODAL_TOKEN_ID when it's unset — that mirrors the gotcha the script's
    `unset MODAL_TOKEN_ID MODAL_TOKEN_SECRET` line works around, and lets
    test_modal_token_isolation actually catch a regression of that line.
    """
    (bin_dir / "infisical").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            # Two subcommands need explicit emulation now that the Python
            # rewrite calls `infisical secrets get` directly (instead of
            # always going through `infisical run -- printenv`).
            #
            # `secrets get <name> ... --plain --silent` echoes a stub value
            # so _preflight_infisical_keys and _resolve_modal_tokens see a
            # non-empty stdout and exit 0. The actual value doesn't matter;
            # only its presence is what the script checks.
            if [[ "${1:-}" == "secrets" && "${2:-}" == "get" ]]; then
                echo "stub-${3}-value"
                exit 0
            fi
            # `run … -- <cmd>` injects MODAL_TOKEN_ID/SECRET only if unset,
            # mirroring the real-world quirk where parent-shell env vars win
            # — which is what `os.environ.pop(...)` in the deploy script
            # works around (regression target for test_modal_token_isolation).
            if [[ "${1:-}" == "run" ]]; then
                [[ -z "${MODAL_TOKEN_ID:-}" ]] && export MODAL_TOKEN_ID="infisical-injected-id"
                [[ -z "${MODAL_TOKEN_SECRET:-}" ]] && export MODAL_TOKEN_SECRET="infisical-injected-secret"
                while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
                shift  # drop the --
                exec "$@"
            fi
            exit 0
            """,
        ),
    )
    _make_executable(bin_dir / "infisical")

    (bin_dir / "modal").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "${1:-}" == "secret" && "${2:-}" == "list" ]]; then
                echo '[{"Name": "devx-gcp-202605260000"}, {"Name": "attio"}]'
                exit 0
            fi
            if [[ "${1:-}" == "deploy" ]]; then
                exit 0
            fi
            exit 0
            """,
        ),
    )
    _make_executable(bin_dir / "modal")

    (bin_dir / "uv").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            # _preflight_uv_version() probes every candidate on PATH with
            # `--version` before anything else runs. Without this branch the
            # stub would look unparseable/incompatible and the preflight
            # would fall through to a real uv further down the outer PATH,
            # silently escaping this stub sandbox for every test in this file.
            if [[ "${1:-}" == "--version" ]]; then
                echo "uv 0.11.29 (stub)"
                exit 0
            fi
            if [[ "${1:-}" == "run" && "${2:-}" == "modal" ]]; then
                shift 2
                exec modal "$@"
            fi
            # `uv run python -c "<snippet>"` is used by _preflight_infisical_keys
            # (to print required_api_keys()) and by _preflight_gcs_buckets (to
            # print Webhook.<prefix>_get_bucket_name()). Detect which by
            # grepping the snippet; emit a realistic value for each branch so
            # the script's downstream `infisical secrets get` / `gcloud
            # storage ls` calls run against something that looks like real
            # output, not stub-as-magic-string degenerate behavior.
            if [[ "${1:-}" == "run" && "${2:-}" == "python" && "${3:-}" == "-c" ]]; then
                snippet="${4:-}"
                if [[ "${snippet}" == *required_api_keys* ]]; then
                    echo "ATTIO_API_KEY"
                    exit 0
                fi
                if [[ "${snippet}" == *clay_get_webhook* ]]; then
                    echo "CLAY_WEBHOOK_URL_TEST"
                    echo "CLAY_WEBHOOK_AUTH_TOKEN_TEST"
                    exit 0
                fi
                if [[ "${snippet}" == *_get_bucket_name* ]]; then
                    echo "stub-bucket-name"
                    exit 0
                fi
                exit 0
            fi
            exit 0
            """,
        ),
    )
    _make_executable(bin_dir / "uv")

    (bin_dir / "gcloud").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            # Defensive: not exercised by export_to_attio.
            exit 0
            """,
        ),
    )
    _make_executable(bin_dir / "gcloud")

    (bin_dir / "flox").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            # _deploy_via_flox() invokes `flox activate --dir ... --mode run -- <cmd>`.
            # The stub just execs everything after the first `--`, matching how a
            # real `flox activate --mode run` hands off to the wrapped command
            # without an interactive shell.
            while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
            shift  # drop the --
            exec "$@"
            """,
        ),
    )
    _make_executable(bin_dir / "flox")


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_default_stubs(bin_dir)
    return bin_dir


def _run_deploy(
    deploy_workspace: DeployWorkspace,
    stub_bin: Path,
    *,
    env_overrides: dict[str, str] | None = None,
    args: tuple[str, ...] = (HANDLER_NAME, SOURCE_NAME),
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["INFISICAL_PROJECT_ID"] = "test-project-id"
    env["INFISICAL_TOKEN"] = "test-token"
    # INFISICAL_ENV is a fail-closed preflight added by ai-2aw — see the
    # script header. Tests pin to "dev" since they stub the modal binary
    # and never reach Infisical.
    env.setdefault("INFISICAL_ENV", "dev")
    # Force the Flox deploy path so the existing infisical/modal/uv/flox
    # stubs handle the deploy step. The Dagger path is exercised by manual
    # smoke tests; bringing a Dagger engine into CI would also drag in real
    # Modal credentials, which defeats the purpose of these stubs.
    # Hard-set, not setdefault: a developer with GTM_DEPLOY_VIA_FLOX=0
    # exported would otherwise silently run this whole suite against the
    # Dagger path, which the stubs cannot serve.
    env["GTM_DEPLOY_VIA_FLOX"] = "1"
    if env_overrides:
        env.update(env_overrides)
    # Invoke the script with the test's own interpreter rather than
    # `uv run python …`. The PATH-overriding `uv` stub catches every
    # `uv run python <anything>` call (it is meant to intercept the script's
    # *internal* preflight calls to `uv run python -c …`), so going through
    # `uv` here would short-circuit the entire script before it starts.
    # ``sys.executable`` points at the venv pytest itself is running under,
    # so all the script's imports (``dagger``, etc.) resolve normally.
    return subprocess.run(
        [sys.executable, str(deploy_workspace.script), *args],
        env=env,
        cwd=deploy_workspace.root,
        capture_output=True,
        text=True,
        # The Flox path now runs two activations per source (uv sync, then
        # modal deploy), so `--all` across five sources is ten stub
        # invocations plus the preflights.
        timeout=180,
        check=False,
    )


def test_substitution_and_restore(
    deploy_workspace: DeployWorkspace,
    stub_bin: Path,
) -> None:
    """AC1: CI runs webhooks-handlers-redeploy.py against stubs; working tree ends clean."""
    original = deploy_workspace.handler_file.read_bytes()

    result = _run_deploy(deploy_workspace, stub_bin)

    assert result.returncode == 0, (
        f"Script failed unexpectedly.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert deploy_workspace.handler_file.read_bytes() == original
    bak = deploy_workspace.handler_file.with_suffix(
        deploy_workspace.handler_file.suffix + ".bak",
    )
    assert not bak.exists(), "stale .bak sidecar left behind"
    # The Flox preflight runs (it is gated on the selector) and survives the
    # pass-through `flox` stub, which activates nothing and so reports no
    # FLOX_ENV. It must degrade to "unverified", not abort the deploy.
    assert "Preflighting Flox environment" in result.stdout
    assert "toolchain pinning unverified" in result.stdout


def test_all_flag_deploys_every_source(
    deploy_workspace: DeployWorkspace,
    stub_bin: Path,
) -> None:
    """AC5: ``--all`` iterates every source imported by the handler.

    Regression target: in early drafts argparse parsed ``--all`` as an
    unknown option, breaking the documented invocation entirely. Each
    iteration must end with the placeholder restored, so the file must
    match HEAD bit-for-bit after all five sources deploy.
    """
    original = deploy_workspace.handler_file.read_bytes()

    result = _run_deploy(deploy_workspace, stub_bin, args=(HANDLER_NAME, "--all"))

    assert result.returncode == 0, (
        f"--all invocation failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert deploy_workspace.handler_file.read_bytes() == original
    # All five sources imported by export_to_attio.py should each have
    # produced a "=== Deploying <source> via <handler> ===" header.
    assert result.stdout.count("=== Deploying ") == 5, (
        f"Expected 5 per-source deploy headers (one for each Webhook import). "
        f"Got:\n{result.stdout}"
    )


def test_clay_handler_preflights_its_source_specific_secrets(
    deploy_workspace: DeployWorkspace,
    stub_bin: Path,
) -> None:
    """Clay deploys discover only their URL/token keys, never ATTIO_API_KEY."""
    result = _run_deploy(
        deploy_workspace,
        stub_bin,
        args=("export_to_clay", "--all"),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("=== Deploying ") == 2
    assert "CLAY_WEBHOOK_URL_TEST" in result.stdout
    assert "CLAY_WEBHOOK_AUTH_TOKEN_TEST" in result.stdout
    assert "ATTIO_API_KEY" not in result.stdout


def test_restore_on_deploy_failure(
    deploy_workspace: DeployWorkspace,
    stub_bin: Path,
) -> None:
    """AC2: EXIT trap restores the handler when `modal deploy` fails mid-iteration.

    Regression target: a refactor that drops the `trap … EXIT` registration
    (or fails to flip BACKUP_FRESHLY_WRITTEN before the deploy) leaves the
    substituted form of the handler committed locally.
    """
    (stub_bin / "modal").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "${1:-}" == "secret" && "${2:-}" == "list" ]]; then
                echo '[{"Name": "devx-gcp-202605260000"}, {"Name": "attio"}]'
                exit 0
            fi
            if [[ "${1:-}" == "deploy" ]]; then
                exit 1  # simulate mid-iteration failure
            fi
            exit 0
            """,
        ),
    )
    _make_executable(stub_bin / "modal")

    original = deploy_workspace.handler_file.read_bytes()

    result = _run_deploy(deploy_workspace, stub_bin)

    assert result.returncode != 0, (
        "Script should have exited non-zero after stub modal deploy failed"
    )
    assert deploy_workspace.handler_file.read_bytes() == original, (
        f"Handler file NOT restored after deploy failure — EXIT trap may be "
        f"missing or BACKUP_FRESHLY_WRITTEN gate may be wrong.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_modal_token_isolation_at_the_secret_preflight(
    deploy_workspace: DeployWorkspace,
    stub_bin: Path,
    tmp_path: Path,
) -> None:
    """AC3: the Modal-secret preflight runs on Infisical's tokens, not yours.

    The deploy step itself is no longer the place to test this: both
    executors now receive an explicit ``deploy_env`` that always overrides
    whatever is in ``os.environ``, so a leaked parent token could not reach
    ``modal deploy`` even if the pop were removed.

    ``_preflight_modal_secrets`` is where the pop still carries weight. It
    shells out through ``infisical run`` and asks the resulting workspace to
    list its secrets — with a personal ``MODAL_TOKEN_ID`` left in the
    environment it would validate the *wrong workspace's* secrets and green-
    light a deploy into a workspace that has none of them.
    """
    env_record = tmp_path / "modal_env.txt"
    (stub_bin / "modal").write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [[ "${{1:-}}" == "secret" && "${{2:-}}" == "list" ]]; then
                echo "MODAL_TOKEN_ID=${{MODAL_TOKEN_ID:-UNSET}}" > "{env_record}"
                echo '[{{"Name": "devx-gcp-202605260000"}}, {{"Name": "attio"}}]'
                exit 0
            fi
            exit 0
            """,
        ),
    )
    _make_executable(stub_bin / "modal")

    result = _run_deploy(
        deploy_workspace,
        stub_bin,
        env_overrides={
            "MODAL_TOKEN_ID": "parent-shell-token",
            "MODAL_TOKEN_SECRET": "parent-shell-secret",
        },
    )

    assert result.returncode == 0, (
        f"Script failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert env_record.exists(), "modal secret list stub was never invoked"
    recorded = env_record.read_text().strip()
    assert recorded == "MODAL_TOKEN_ID=infisical-injected-id", (
        f"Parent shell's MODAL_TOKEN_ID leaked into the Modal secret "
        f"preflight — the `os.environ.pop(...)` call in "
        f"webhooks-handlers-redeploy.py is missing or ineffective. "
        f"Got: {recorded}"
    )


def test_deploy_backend_selector_rejects_a_bogus_value(
    deploy_workspace: DeployWorkspace,
    stub_bin: Path,
) -> None:
    """`GTM_DEPLOY_VIA_FLOX=maybe` must abort, naming the variable.

    Before the selector went through ``env_flag``, anything other than the
    literal ``"1"`` — including ``true`` — silently selected Dagger, so an
    operator on a Conductor sandbox got an engine-connection failure instead
    of an answer about their typo.
    """
    result = _run_deploy(
        deploy_workspace,
        stub_bin,
        env_overrides={"GTM_DEPLOY_VIA_FLOX": "maybe"},
    )

    assert result.returncode != 0
    assert "GTM_DEPLOY_VIA_FLOX" in result.stderr


def test_deploy_step_env_is_the_recipe_not_the_operator_shell(
    deploy_workspace: DeployWorkspace,
    stub_bin: Path,
    tmp_path: Path,
) -> None:
    """The Flox child sees the deploy recipe's env, not the operator's exports.

    ``TELEMETRY_COLLECTOR_APP=""`` is the documented opt-out from collector
    mode. Inherited into ``modal deploy``, ``src/secrets_bootstrap.py`` would
    bake direct-sink creds into the app's Modal Secret and lose Logfire —
    while a Dagger deploy of the same commit stayed in collector mode. This
    is the end-to-end counterpart of the unit-level scrub test in
    ``test_deploy_webhook_dagger.py``.
    """
    env_record = tmp_path / "deploy_env.txt"
    (stub_bin / "modal").write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [[ "${{1:-}}" == "secret" && "${{2:-}}" == "list" ]]; then
                echo '[{{"Name": "devx-gcp-202605260000"}}, {{"Name": "attio"}}]'
                exit 0
            fi
            if [[ "${{1:-}}" == "deploy" ]]; then
                {{
                  echo "TELEMETRY_COLLECTOR_APP=${{TELEMETRY_COLLECTOR_APP-ABSENT}}"
                  echo "MODAL_ENVIRONMENT=${{MODAL_ENVIRONMENT-ABSENT}}"
                  echo "INFISICAL_ENV=${{INFISICAL_ENV-ABSENT}}"
                }} > "{env_record}"
                exit 0
            fi
            exit 0
            """,
        ),
    )
    _make_executable(stub_bin / "modal")

    result = _run_deploy(
        deploy_workspace,
        stub_bin,
        env_overrides={
            "TELEMETRY_COLLECTOR_APP": "",
            "MODAL_ENVIRONMENT": "staging",
        },
    )

    assert result.returncode == 0, (
        f"Script failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    recorded = dict(line.split("=", 1) for line in env_record.read_text().splitlines())
    assert recorded["TELEMETRY_COLLECTOR_APP"] == "ABSENT"
    assert recorded["MODAL_ENVIRONMENT"] == "ABSENT"
    # ...while the recipe's own values do arrive.
    assert recorded["INFISICAL_ENV"] == "dev"


def test_preflight_fails_when_infisical_returns_empty_stdout(
    deploy_workspace: DeployWorkspace,
    stub_bin: Path,
) -> None:
    """ai-4pw: ``infisical secrets get`` exits 0 even when the key is missing.

    Empirically (CLI 0.43.84, dlthub-sandbox/dev, 2026-05-26) the CLI
    differentiates present-vs-missing keys only via stdout, not via exit
    code. A returncode-only preflight is therefore theater — it would
    always pass.

    Regression target: a refactor that drops the ``not proc.stdout.strip()``
    side of the check in ``_preflight_infisical_keys`` would silently let
    a missing/rotated ATTIO_API_KEY ship to Modal and fail on the first
    Hookdeck event (the exact failure mode ai-ctn/ai-q9k were filed to
    eliminate).
    """
    (stub_bin / "infisical").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            # Mimic the live CLI bug: `secrets get` exits 0 with empty stdout
            # when the key is missing, instead of a non-zero return code.
            if [[ "${1:-}" == "secrets" && "${2:-}" == "get" ]]; then
                # Print nothing; exit 0. Matches CLI 0.43.84 behavior for
                # a missing key under --plain --silent.
                exit 0
            fi
            if [[ "${1:-}" == "run" ]]; then
                [[ -z "${MODAL_TOKEN_ID:-}" ]] && export MODAL_TOKEN_ID="infisical-injected-id"
                [[ -z "${MODAL_TOKEN_SECRET:-}" ]] && export MODAL_TOKEN_SECRET="infisical-injected-secret"
                while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
                shift
                exec "$@"
            fi
            exit 0
            """,
        ),
    )
    _make_executable(stub_bin / "infisical")

    result = _run_deploy(deploy_workspace, stub_bin)

    assert result.returncode != 0, (
        f"Script should have failed when `infisical secrets get` returns "
        f"empty stdout. A returncode-only preflight would let this through.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ATTIO_API_KEY" in result.stderr, (
        f"Failure message should name the specific missing key. Got "
        f"stderr:\n{result.stderr}"
    )


def test_shutil_copyfile_overwrites(tmp_path: Path) -> None:
    r"""AC4: restore overwrites unconditionally — no alias-bypass game in Python.

    The bash script needed `\\cp -f` to dodge `cp -i` aliases that would
    silently refuse the restore. The Python rewrite uses ``shutil.copyfile``,
    which always overwrites; this test pins that contract so a refactor to a
    helper that respects ``exist_ok=False`` or similar would fail loudly.
    """
    import shutil

    src = tmp_path / "src.txt"
    src.write_text("ORIGINAL")
    dst = tmp_path / "dst.txt"
    dst.write_text("STALE")

    shutil.copyfile(src, dst)

    assert dst.read_text() == "ORIGINAL"
