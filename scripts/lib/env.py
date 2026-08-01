"""Shared helpers for repo-local command-line scripts.

Keep the execution contract in one place so operators and agents see the same
repo-approved invocation form. That contract has two halves: how a script is
invoked (:func:`infisical_run_example`) and *where its external tooling runs*
(:class:`ExecBackend` and friends).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPT_DIR.parent


def infisical_run_example(
    script_relpath: str,
    *,
    env_placeholder: str = "<dev|prod>",
    extra_args: str = "",
) -> str:
    """Return the canonical `infisical run` form for a repo script."""

    suffix = f" {extra_args}" if extra_args else ""
    return (
        'infisical run --projectId "$INFISICAL_PROJECT_ID" '
        '--token "$INFISICAL_TOKEN" '
        f"--env={env_placeholder} -- {script_relpath}{suffix}"
    )


def add_repo_root_to_sys_path() -> None:
    """Insert the repo root into `sys.path` if a script needs local imports."""

    repo_root = str(REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def clean_env(value: str | None) -> str | None:
    """Strip whitespace from an env value; treat blank-after-strip as ``None``.

    Trailing newlines on secrets (e.g. from a `cat`-ed file or copy-paste)
    silently break auth otherwise — Attio rejects "Bearer key\\n" with a 401
    that looks identical to a bad key. Shared by repo scripts that bootstrap
    secrets from the environment or `.env.local`.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse the subset of `.env` syntax repo scripts care about.

    Supports blank lines, `# comments`, a leading `export` keyword, and
    single-/double-quoted values (with inline `# comment` after an *unquoted*
    value). Does NOT support multiline values or shell expansion — `.env.local`
    here only carries Infisical creds, which are single-line opaque tokens.
    """
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        if value and value[0] in ("'", '"'):
            # Quoted: take everything up to the matching closing quote and
            # discard the rest (e.g. a trailing ` # comment`). A `#` inside the
            # quotes is preserved. An unterminated quote keeps the remainder.
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            comment_idx = value.find(" #")
            if comment_idx >= 0:
                value = value[:comment_idx].rstrip()
        parsed[key] = value
    return parsed


def read_infisical_credentials() -> tuple[str, str] | None:
    """Resolve INFISICAL_PROJECT_ID/TOKEN from env, then ``REPO_ROOT/.env.local``.

    We deliberately avoid asking the operator to `set -a; source .env.local`
    (per repo memory) — instead we parse the file ourselves and feed the values
    straight to `infisical run` as CLI flags. Returns ``None`` when neither the
    environment nor `.env.local` supplies both values.

    The two credentials are treated as an ATOMIC PAIR per source: the
    environment is used only when it supplies BOTH; otherwise both values come
    from `.env.local`. Mixing one value from each source could silently target
    the wrong workspace or fail auth in a non-obvious way.
    """
    env_project_id = clean_env(os.environ.get("INFISICAL_PROJECT_ID"))
    env_token = clean_env(os.environ.get("INFISICAL_TOKEN"))
    if env_project_id and env_token:
        return env_project_id, env_token

    env_file = REPO_ROOT / ".env.local"
    if not env_file.is_file():
        return None

    parsed = parse_dotenv(env_file.read_text())
    file_project_id = clean_env(parsed.get("INFISICAL_PROJECT_ID"))
    file_token = clean_env(parsed.get("INFISICAL_TOKEN"))
    if file_project_id and file_token:
        return file_project_id, file_token
    return None


class ExecBackend(StrEnum):
    """Where a repo script runs the external tooling it shells out to.

    ``dagger`` and ``flox`` are two answers to the same requirement — a
    *pinned* toolchain — reached by different mechanisms: an OCI image pinned
    by digest, or ``.flox/env/manifest.lock``. ``host`` deliberately provides
    no pinning at all and exists only for the stub-binary tests, which prepend
    fake ``infisical``/``modal``/``uv`` executables to ``PATH`` that a Flox
    activation would shadow with the real ones.
    """

    DAGGER = "dagger"
    FLOX = "flox"
    HOST = "host"


EXEC_BACKEND_ENV = "GTM_EXEC_BACKEND"

# Superseded by EXEC_BACKEND_ENV. Kept as an alias because it was documented
# operator-to-operator in AGENTS.md and webhooks/README.md. The name was always
# wrong -- it never implied a dry run, it really deployed to Modal.
LEGACY_DRY_RUN_ENV = "DAGGER_DRY_RUN"

# `.flox/env.json`'s "name". Flox derives the realized directory from it.
FLOX_ENV_NAME = "gtm-sdk"


class FloxBackendError(RuntimeError):
    """Raised when ``flox`` is selected but the environment cannot serve it."""


def resolve_exec_backend(env: Mapping[str, str] | None = None) -> ExecBackend:
    """Pick the execution backend from the environment.

    Defaults to ``dagger`` when nothing is set. Auto-detecting the sandbox is
    tempting but would silently change behaviour on a Mac, so selection is
    explicit -- the same posture ``INFISICAL_ENV`` takes.

    Raises ``ValueError`` on an unrecognised value rather than falling back:
    a typo'd backend name must not quietly become the default.
    """
    environ = os.environ if env is None else env

    raw = clean_env(environ.get(EXEC_BACKEND_ENV))
    if raw is not None:
        try:
            return ExecBackend(raw.lower())
        except ValueError:
            valid = ", ".join(backend.value for backend in ExecBackend)
            msg = f"{EXEC_BACKEND_ENV}={raw!r} is not one of: {valid}"
            raise ValueError(msg) from None

    if clean_env(environ.get(LEGACY_DRY_RUN_ENV)) == "1":
        print(
            f"warning: {LEGACY_DRY_RUN_ENV}=1 is deprecated. Use "
            f"{EXEC_BACKEND_ENV}={ExecBackend.HOST} for the bare-PATH path, or "
            f"{EXEC_BACKEND_ENV}={ExecBackend.FLOX} on Conductor cloud "
            "sandboxes to get the toolchain pinned by "
            ".flox/env/manifest.lock.",
            file=sys.stderr,
        )
        return ExecBackend.HOST

    return ExecBackend.DAGGER


def flox_bin_dir() -> Path:
    """Return the ``bin/`` of the realized Flox environment.

    Mirrors the ``FLOX_BIN`` derivation in
    ``scripts/conductor-workspace-setup.sh`` -- keep the two in lockstep.
    ``platform.machine()`` reports ``arm64`` on Apple silicon where Flox names
    the directory ``aarch64``, which is what that script's
    ``sed s/arm64/aarch64/`` is for.
    """
    machine = platform.machine()
    arch = "aarch64" if machine == "arm64" else machine
    system = platform.system().lower()
    return REPO_ROOT / ".flox" / "run" / f"{arch}-{system}.{FLOX_ENV_NAME}-run" / "bin"


def flox_activate_prefix() -> list[str]:
    """Return the argv prefix that runs a command inside the Flox environment.

    ``--mode run`` is not optional: Flox refuses to activate an environment in
    dev mode while another shell (e.g. an agent's) holds a run-mode activation
    of the same environment.
    """
    return ["flox", "activate", "--dir", str(REPO_ROOT), "--mode", "run", "--"]


def _flox_backend_error(reason: str) -> str:
    return (
        f"{EXEC_BACKEND_ENV}={ExecBackend.FLOX} was requested but {reason}. "
        "Provision it with `bash scripts/conductor-workspace-setup.sh`, or add "
        "a missing tool to .flox/env/manifest.toml via `flox install <pkg>` "
        "(never hand-edit manifest.lock). To run against bare PATH instead, "
        f"set {EXEC_BACKEND_ENV}={ExecBackend.HOST} -- but note that gives up "
        "the pinning this backend exists to provide."
    )


def preflight_flox(required_tools: Sequence[str] = ()) -> Path:
    """Materialize and verify the Flox environment; return its ``bin/``.

    Raises :class:`FloxBackendError` rather than degrading to bare ``PATH``.
    Silent degradation is the specific failure mode worth refusing here: the
    point of this backend is a pinned toolchain, so falling through to whatever
    happens to be installed would report success while delivering the opposite
    of what was asked for. A Conductor sandbox can very plausibly be in that
    state -- ``scripts/conductor-workspace-setup.sh`` falls back to curl
    installers when Flox provisioning fails, leaving ``.flox/run/`` empty.
    """
    if shutil.which("flox") is None:
        raise FloxBackendError(_flox_backend_error("`flox` is not on PATH"))

    # Downloads the pinned store paths on a fresh sandbox; a no-op once
    # realized. This is also what creates `.flox/run/`, so it has to run
    # before the directory check below.
    probe = subprocess.run(  # noqa: S603
        [*flox_activate_prefix(), "true"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip()
        raise FloxBackendError(
            _flox_backend_error(f"`flox activate` failed: {detail}"),
        )

    bin_dir = flox_bin_dir()
    if not bin_dir.is_dir():
        raise FloxBackendError(
            _flox_backend_error(f"the environment realized but {bin_dir} is absent"),
        )

    missing = [tool for tool in required_tools if not (bin_dir / tool).exists()]
    if missing:
        raise FloxBackendError(
            _flox_backend_error(f"{', '.join(missing)} is absent from {bin_dir}"),
        )

    return bin_dir


def wrap_for_backend(
    argv: Sequence[str],
    backend: ExecBackend,
    *,
    required_tools: Sequence[str] = (),
) -> list[str]:
    """Return ``argv`` ready to run under ``backend``.

    Preflights and prefixes a Flox activation for ``flox``; returns ``argv``
    untouched for ``host``. ``dagger`` never reaches here -- it does not shell
    out to a host command at all.
    """
    if backend is ExecBackend.FLOX:
        preflight_flox(required_tools)
        return [*flox_activate_prefix(), *argv]
    return list(argv)
