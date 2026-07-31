"""Re-exec direct script entrypoints through a compatible project ``uv``.

Scripts using a ``uv run`` shebang cannot recover when the first ``uv`` on
PATH violates this repo's required-version constraint: that executable rejects
the command before Python starts.  Entry points therefore use the system
Python shebang, import this stdlib-only module before third-party imports, and
let it select a compatible ``uv`` binary.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal, NoReturn

from scripts.lib.uv_resolve import NoCompatibleUvError, find_compatible_uv_for_repo

RunMode = Literal["python", "script"]

UV_BOOTSTRAP_ENV = "_GTM_UV_BOOTSTRAPPED"


def _fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def bootstrap_uv(*, script_path: str, mode: RunMode) -> None:
    """Re-exec ``script_path`` through a compatible ``uv`` when run directly.

    Imports in unit tests must never replace the pytest process, and an active
    virtualenv has already selected a concrete interpreter.  In both cases the
    bootstrap is intentionally inert.  A sentinel prevents an accidental
    re-exec loop if ``uv`` itself delegates back to this entrypoint.
    """
    if os.environ.get(UV_BOOTSTRAP_ENV) or sys.prefix != sys.base_prefix:
        return

    resolved_script = Path(script_path).resolve()
    repo_root = resolved_script.parents[1]
    try:
        candidate = find_compatible_uv_for_repo(cwd=str(repo_root))
    except NoCompatibleUvError as exc:
        _fail(str(exc))

    os.environ[UV_BOOTSTRAP_ENV] = "1"
    os.chdir(repo_root)
    command = [candidate.path, "run"]
    if mode == "python":
        command.extend(("--project", str(repo_root), "python"))
    else:
        command.append("--script")
    command.extend((str(resolved_script), *sys.argv[1:]))
    # `execv` preserves the process exit code and signal handling. `candidate`
    # is resolved to an absolute executable path by uv_resolve.
    # trunk-ignore(bandit/B606): argv is a resolved uv binary plus this script's arguments
    os.execv(candidate.path, command)  # noqa: S606
    msg = "os.execv() returned unexpectedly"
    raise AssertionError(msg)  # pragma: no cover
