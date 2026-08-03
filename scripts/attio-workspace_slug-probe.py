#!/usr/bin/env python3
r"""Probe the Attio workspace slug for an `ATTIO_API_KEY`.

Calls `GET /v2/self` through the repo's own Attio adapter (`libs.attio`). The
endpoint returns the workspace the token authenticates against, which is the
authoritative way to map an API key to its workspace slug — Infisical doesn't
store the slug as its own secret.

This used to run `curl` inside a Dagger-managed Alpine container, which bought
nothing the SDK does not already do (the round trip is identical) while
costing an engine, a container image, and a scrypt-derived cache tag to defeat
Dagger's exec cache — that cache once returned a *dev* workspace slug after
the operator switched to prod, which is precisely the answer this script
exists to get right.

The Infisical environment is explicit (no silent prod default): pass `--env`
or set `INFISICAL_ENV`. The script auto-bootstraps `infisical run` when
`ATTIO_API_KEY` isn't already set by reading `gtm-sdk/.env.local` for the
Infisical project + token.

Usage:

    scripts/attio-workspace_slug-probe.py --env dev
    scripts/attio-workspace_slug-probe.py --env prod
    scripts/attio-workspace_slug-probe.py --env dev --json

You can still pre-inject the key yourself if you don't want the self-bootstrap:

    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \\
      --env=prod -- scripts/attio-workspace_slug-probe.py
"""

from __future__ import annotations

import pathlib as _pathlib
import sys as _sys

_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
from scripts.lib.uv_bootstrap import bootstrap_uv as _bootstrap_uv  # noqa: E402

if __name__ == "__main__":
    _bootstrap_uv(script_path=__file__, mode="python")

import json
import os
import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import click
import typer

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.attio.client import get_client  # noqa: E402
from libs.attio.sdk_boundary import (  # noqa: E402
    describe_attio_error,
    model_dump_or_empty,
)
from scripts.lib.env import (  # noqa: E402
    clean_env,
    infisical_run_example,
    read_infisical_credentials,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# Sentinel propagated through `os.execvp` -> `infisical run` -> child python.
# Prevents an infinite re-bootstrap loop when the chosen Infisical env simply
# does not contain `ATTIO_API_KEY` (the child would otherwise see an empty key
# and call _bootstrap_via_infisical() again, ad infinitum).
_BOOTSTRAP_SENTINEL_ENV = "_ATTIO_PROBE_BOOTSTRAPPED"
app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=__doc__,
)


class InfisicalEnv(StrEnum):
    dev = "dev"
    prod = "prod"


class AttioProbeError(RuntimeError):
    """Raised when the /v2/self request fails or returns an unusable body."""


def extract_workspace_slug(body: str) -> str:
    """Pull `workspace_slug` from a /v2/self response body.

    Raises ValueError with the offending payload when the field is missing,
    the response isn't a JSON object, or the body isn't valid JSON at all
    (e.g. an upstream proxy returning an HTML 502 page). All three failure
    modes flow through `main()`'s `except (AttioProbeError, ValueError)` to
    produce a clean stderr line instead of a traceback.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"/v2/self response was not valid JSON: {body!r}",
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"/v2/self response was not a JSON object: {payload!r}",
        )
    slug = payload.get("workspace_slug")
    if not isinstance(slug, str) or not slug:
        raise ValueError(
            f"/v2/self response did not include a workspace_slug: {payload!r}",
        )
    return slug


def probe(*, api_key: str, json_output: bool) -> str:
    """Return the workspace slug, or the whole /v2/self payload as JSON.

    One round trip serves both modes, as the container did: the response is
    dumped once and either pretty-printed or fed to
    :func:`extract_workspace_slug`. That helper keeps its string-in signature
    deliberately — it predates the SDK rewrite and its tests carry the
    accumulated knowledge of how /v2/self malforms (inactive tokens, proxy
    HTML, non-object JSON), which is worth more than a tidier interface.
    """
    try:
        with get_client(api_key) as client:
            identity = client.meta.get_v2_self()
    except Exception as exc:  # noqa: BLE001 — every SDK failure is one message
        # The SDK's `Code` Literal omits most of Attio's real error codes, so
        # the body is only legible via describe_attio_error's re-parse.
        described = describe_attio_error(exc)
        detail = f"{described.code}: {described.message}" if described else str(exc)
        message = f"/v2/self request failed: {detail}"
        raise AttioProbeError(message) from exc

    payload = model_dump_or_empty(identity)
    if json_output:
        return json.dumps(payload, indent=2, default=str)
    return extract_workspace_slug(json.dumps(payload, default=str))


def _bootstrap_via_infisical(env: str, forward_args: list[str]) -> int:
    # If we've already bootstrapped once and still have no ATTIO_API_KEY, the
    # secret is simply absent from this Infisical env — re-execing would loop
    # forever. Fail fast with an actionable message instead.
    if os.environ.get(_BOOTSTRAP_SENTINEL_ENV):
        print(
            f"ATTIO_API_KEY is not present in the Infisical '{env}' environment.\n"
            "Verify the secret exists at that env, or pass --env to switch.",
            file=sys.stderr,
        )
        return 2

    creds = read_infisical_credentials()
    if creds is None:
        print(
            "ATTIO_API_KEY is not set and INFISICAL_PROJECT_ID/INFISICAL_TOKEN\n"
            f"were not found in the environment or {REPO_ROOT / '.env.local'}.\n"
            "Run via:\n"
            f"  {infisical_run_example('scripts/attio-workspace_slug-probe.py')}",
            file=sys.stderr,
        )
        return 2

    project_id, token = creds
    argv = [
        "infisical",
        "run",
        "--projectId",
        project_id,
        "--token",
        token,
        f"--env={env}",
        "--",
        sys.executable,
        str(Path(__file__).resolve()),
        *forward_args,
    ]
    # Mark the child so it can detect a missing ATTIO_API_KEY and exit cleanly
    # instead of recursively re-bootstrapping. Set on os.environ (not just the
    # local dict) because execvp inherits the current process env.
    os.environ[_BOOTSTRAP_SENTINEL_ENV] = "1"
    # trunk-ignore(bandit/B606): argv is built from local config + the script's own path
    os.execvp(argv[0], argv)  # noqa: S606
    return 0


def _run(*, env: InfisicalEnv | None, json_output: bool) -> int:
    api_key = clean_env(os.environ.get("ATTIO_API_KEY"))
    if not api_key:
        # The Infisical env is only needed when we're going to bootstrap; if
        # the operator pre-injected ATTIO_API_KEY (e.g. via another secret
        # manager or a direct shell export), we should run with that key as
        # documented.
        infisical_env = (
            env.value
            if env is not None
            else clean_env(
                os.environ.get("INFISICAL_ENV"),
            )
        )
        if infisical_env not in {"dev", "prod"}:
            print(
                "Infisical environment is required to bootstrap ATTIO_API_KEY. "
                "Pass --env=dev|prod or set INFISICAL_ENV. (Refusing to default "
                "to prod silently — running against the wrong env returns the "
                "wrong workspace slug.)",
                file=sys.stderr,
            )
            return 2
        forward = [f"--env={infisical_env}"]
        if json_output:
            forward.append("--json")
        return _bootstrap_via_infisical(infisical_env, forward)

    try:
        output = probe(api_key=api_key, json_output=json_output)
    except (AttioProbeError, ValueError) as exc:
        # ValueError covers extract_workspace_slug raising on inactive tokens
        # where Attio omits workspace_slug entirely.
        print(f"attio probe failed: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


@app.command()
def cli(
    env: InfisicalEnv | None = typer.Option(
        None,
        "--env",
        help=(
            "Infisical environment to read ATTIO_API_KEY from. "
            "Required unless INFISICAL_ENV is set. There is no silent default — "
            "a wrong env returns a different workspace slug with no warning."
        ),
    ),
    json_output: bool = typer.Option(  # noqa: FBT001
        False,  # noqa: FBT003
        "--json",
        help="Print the full /v2/self JSON payload instead of just the slug.",
    ),
) -> int:
    return _run(env=env, json_output=json_output)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = app(
            args=list(argv) if argv is not None else None,
            standalone_mode=False,
        )
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        typer.echo(exc.code, err=True)
        return 1
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
