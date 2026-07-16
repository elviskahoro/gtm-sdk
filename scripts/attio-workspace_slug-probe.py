#!/usr/bin/env -S uv run python
"""Probe the Attio workspace slug for an `ATTIO_API_KEY`.

Calls `GET https://api.attio.com/v2/self` inside a Dagger-managed Alpine
container so the host machine doesn't need `curl` and the API key is passed in
as a Dagger secret (kept out of container layer history). The endpoint returns
the workspace the token authenticates against, which is the authoritative way
to map an API key to its workspace slug — Infisical doesn't store the slug as
its own secret.

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

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import dagger

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.env import infisical_run_example  # noqa: E402

# Sentinel propagated through `os.execvp` -> `infisical run` -> child python.
# Prevents an infinite re-bootstrap loop when the chosen Infisical env simply
# does not contain `ATTIO_API_KEY` (the child would otherwise see an empty key
# and call _bootstrap_via_infisical() again, ad infinitum).
_BOOTSTRAP_SENTINEL_ENV = "_ATTIO_PROBE_BOOTSTRAPPED"


class AttioProbeError(RuntimeError):
    """Raised when the /v2/self request fails or returns an unusable body."""


# Runs inside the container. Curls /v2/self with the injected secret and emits
# the raw JSON body. Parsing (slug extraction, pretty-printing) happens in
# Python so we can unit-test it without standing up a Dagger engine.
PROBE_SCRIPT = r"""#!/usr/bin/env sh
set -eu

: "${ATTIO_API_KEY:?ATTIO_API_KEY not set in container}"

# --fail-with-body: non-2xx -> exit non-zero AND keep the body on stdout so we
# can surface Attio's error message instead of a bare "curl: (22)".
curl -sS --fail-with-body \
  -H "Authorization: Bearer ${ATTIO_API_KEY}" \
  https://api.attio.com/v2/self
"""


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


async def probe(*, api_key: str, json_output: bool) -> str:
    # Dagger's exec cache key does not include the secret value — two runs with
    # the same secret *name* but different *values* (e.g. dev vs prod
    # ATTIO_API_KEY) will return the previously cached stdout and silently
    # mislead the operator (we hit this returning a dev workspace slug after
    # switching to prod). Derive a stable per-key tag and bind it both as the
    # secret's Dagger name and an env var so the cache key changes with the
    # key. The tag is a truncated SHA-256, so it can't be reversed to recover
    # the key from the trace.
    #
    # `usedforsecurity=False` tells static analyzers (CodeQL, bandit) that this
    # is a cache-key derivation, not password storage — SHA-256 is the right
    # primitive here precisely because we want a deterministic, non-reversible
    # tag, not a slow KDF.
    key_tag = hashlib.sha256(
        api_key.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:16]

    async with dagger.connection(dagger.Config(log_output=sys.stderr)):
        api_secret = dagger.dag.set_secret(f"attio-api-key-{key_tag}", api_key)

        container = (
            dagger.dag.container()
            .from_("alpine:3.20")
            .with_exec(["apk", "add", "--no-cache", "curl", "ca-certificates"])
            .with_new_file("/work/probe.sh", contents=PROBE_SCRIPT, permissions=0o755)
        )

        executed = (
            container.with_secret_variable("ATTIO_API_KEY", api_secret)
            .with_env_variable("ATTIO_API_KEY_TAG", key_tag)
            .with_exec(["/work/probe.sh"])
        )

        try:
            body = await executed.stdout()
        except dagger.ExecError as exc:
            # curl --fail-with-body writes the Attio error body to stdout before
            # exiting non-zero, so prefer that over the bare Dagger message.
            attio_body = (exc.stdout or "").strip()
            container_stderr = (exc.stderr or "").strip()
            detail = attio_body or container_stderr or str(exc)
            raise AttioProbeError(f"/v2/self request failed: {detail}") from exc

    if json_output:
        try:
            return json.dumps(json.loads(body), indent=2)
        except json.JSONDecodeError as exc:
            raise AttioProbeError(
                f"/v2/self returned non-JSON body: {body!r}",
            ) from exc
    return extract_workspace_slug(body)


def _clean_env(value: str | None) -> str | None:
    """Strip whitespace from an env value and treat blank-after-strip as None.

    Trailing newlines on secrets (e.g. from a `cat`-ed file or copy-paste)
    silently break auth otherwise — Attio rejects "Bearer key\\n" with a 401
    that looks identical to a bad key.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _read_infisical_credentials() -> tuple[str, str] | None:
    """Resolve INFISICAL_PROJECT_ID/TOKEN from env, then `gtm-sdk/.env.local`.

    We deliberately avoid asking the operator to `set -a; source .env.local`
    (per repo memory) — instead we parse the file ourselves and feed the
    values straight to `infisical run` as CLI flags.
    """
    project_id = _clean_env(os.environ.get("INFISICAL_PROJECT_ID"))
    token = _clean_env(os.environ.get("INFISICAL_TOKEN"))
    if project_id and token:
        return project_id, token

    env_file = REPO_ROOT / ".env.local"
    if not env_file.is_file():
        return None

    parsed = _parse_dotenv(env_file.read_text())

    project_id = project_id or _clean_env(parsed.get("INFISICAL_PROJECT_ID"))
    token = token or _clean_env(parsed.get("INFISICAL_TOKEN"))
    if project_id and token:
        return project_id, token
    return None


def _parse_dotenv(text: str) -> dict[str, str]:
    """Parse the subset of `.env` syntax we care about.

    Supports:
      - blank lines and comments (`# ...`)
      - leading `export` keyword (`export KEY=value`)
      - double- and single-quoted values
      - inline `# comment` after an *unquoted* value

    Does NOT support multiline values or shell expansion — `.env.local` here
    only carries Infisical creds, which are single-line opaque tokens.
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
        # Quoted: keep everything between matching quotes verbatim, ignore
        # any trailing `# comment`. Unquoted: strip an inline comment.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            comment_idx = value.find(" #")
            if comment_idx >= 0:
                value = value[:comment_idx].rstrip()
        parsed[key] = value
    return parsed


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

    creds = _read_infisical_credentials()
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default=None,
        help=(
            "Infisical environment to read ATTIO_API_KEY from. "
            "Required unless INFISICAL_ENV is set. There is no silent default — "
            "a wrong env returns a different workspace slug with no warning."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full /v2/self JSON payload instead of just the slug.",
    )
    args = parser.parse_args()

    api_key = _clean_env(os.environ.get("ATTIO_API_KEY"))
    if not api_key:
        # The Infisical env is only needed when we're going to bootstrap; if
        # the operator pre-injected ATTIO_API_KEY (e.g. via another secret
        # manager or a direct shell export), we should run with that key as
        # documented.
        env = args.env or _clean_env(os.environ.get("INFISICAL_ENV"))
        if env not in {"dev", "prod"}:
            print(
                "Infisical environment is required to bootstrap ATTIO_API_KEY. "
                "Pass --env=dev|prod or set INFISICAL_ENV. (Refusing to default "
                "to prod silently — running against the wrong env returns the "
                "wrong workspace slug.)",
                file=sys.stderr,
            )
            return 2
        forward = [f"--env={env}"]
        if args.json:
            forward.append("--json")
        return _bootstrap_via_infisical(env, forward)

    try:
        output = asyncio.run(probe(api_key=api_key, json_output=args.json))
    except (AttioProbeError, ValueError) as exc:
        # ValueError covers extract_workspace_slug raising on inactive tokens
        # where Attio omits workspace_slug entirely.
        print(f"attio probe failed: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
