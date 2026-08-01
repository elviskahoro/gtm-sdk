#!/usr/bin/env python3
r"""Dump all Hookdeck events attached to a single connection.

By default, runs the `hookdeck` CLI inside a Dagger-managed container so the
dump is reproducible and the host machine does not need `hookdeck`/`jq`
installed. Authenticates to Hookdeck headlessly via `hookdeck ci --api-key
...`, paginates `event list --connection-id`, and writes one `<event-id>.json`
(metadata) plus one `<event-id>.body` (raw request body) per event to the
host output dir.

Set `GTM_HOOKDECK_DUMP_VIA_FLOX=1` to instead run the same dump script via a
Flox-activated host shell (`scripts/lib/flox.py::flox_activate_prefix()`) --
the fallback for Conductor cloud sandboxes, where Dagger's container engine
cannot start at all (issue #284; do not reinvestigate). `jq` comes from
`.flox/env/manifest.toml`. `hookdeck-cli` does NOT: its npm package is a thin
postinstall wrapper around a prebuilt Go binary published on GitHub releases,
so the Flox path downloads that same binary directly (pinned version,
checksum-verified) into a scratch prefix under `tmp/hookdeck-dump-bin/` --
never a system-wide install, and no Node/npm dependency at all. Delete that
directory to force a re-download. See AGENTS.md's "Dagger-fallback pattern
(Flox)" section for the pattern shared with
`scripts/webhooks-handlers-redeploy.py` and `scripts/pr-review-threads.py`.

You can identify the connection either by ID (`--connection-id web_xxx`) or by
its human name (`--connection-name rb2b-visits-mock`). Name lookups happen
inside the container (or, under the Flox fallback, via the same script run on
the host) so the host still needs no Hookdeck install of its own.

Usage:
    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=<dev|prod> -- scripts/hookdeck-connection_events-dump.py \\
        --connection-id web_xxx

    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=<dev|prod> -- scripts/hookdeck-connection_events-dump.py \\
        --connection-name rb2b-visits-mock

    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=<dev|prod> -- scripts/hookdeck-connection_events-dump.py \\
        --connection-id web_xxx --max-events 50

Requires `HOOKDECK_API_KEY` in the environment (inject via Infisical).
"""

from __future__ import annotations

import pathlib as _pathlib
import sys as _sys

_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
from scripts.lib.uv_bootstrap import bootstrap_uv as _bootstrap_uv  # noqa: E402

if __name__ == "__main__":
    _bootstrap_uv(script_path=__file__, mode="python")

import argparse
import asyncio
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import dagger

from scripts.lib.env import env_flag
from scripts.lib.flox import flox_activate_prefix

# Anchor on the script's directory so relative output paths resolve correctly
# regardless of the CWD `uv run` was invoked from.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "out" / "hookdeck-events"

# Pinned so the Flox executor's downloaded binary is reproducible run to run,
# matching this repo's habit of pinning exact toolchain versions elsewhere
# (e.g. DAGGER_BASE_IMAGE, the `uv` version in .flox/env/manifest.toml).
HOOKDECK_CLI_VERSION = "2.3.1"
HOOKDECK_RELEASE_BASE = (
    f"https://github.com/hookdeck/hookdeck-cli/releases/download/"
    f"v{HOOKDECK_CLI_VERSION}"
)

# Scratch dir under tmp/ (gitignored) -- never a system-wide install or the
# operator's own environment. Mirrors FLOX_DEPLOY_VENV in
# scripts/webhooks-handlers-redeploy.py. Shared across invocations (it's an
# idempotent install cache keyed on the binary's presence), unlike the
# per-invocation work dir created fresh in _dump_via_flox. Scoped by version
# so bumping HOOKDECK_CLI_VERSION uses a fresh directory instead of silently
# keeping whatever binary an older version of this script installed here.
FLOX_HOOKDECK_BIN_PREFIX = (
    REPO_ROOT / "tmp" / "hookdeck-dump-bin" / HOOKDECK_CLI_VERSION
)

# Shell script executed inside the container. Kept here so the dump logic ships
# with the Python entrypoint and is reviewable in one file. Paginates the event
# list, then per-event fetches `event get` (metadata) and `event raw-body`.
DUMP_SCRIPT = r"""#!/usr/bin/env bash
set -euo pipefail

: "${HOOKDECK_API_KEY:?HOOKDECK_API_KEY not set in container}"

LIMIT="${LIMIT_PER_PAGE:-100}"
MAX="${MAX_EVENTS:-}"
CONNECTION_ID="${CONNECTION_ID:-}"
CONNECTION_NAME="${CONNECTION_NAME:-}"
# Defaults match the Dagger container's ephemeral filesystem layout. The Flox
# executor overrides both to real host paths so events land straight in the
# final output dir with no export step.
OUT_DIR="${OUT_DIR:-/out}"
HD_TMP_DIR="${HD_TMP_DIR:-/tmp/hd}"

mkdir -p "$OUT_DIR"
mkdir -p "$HD_TMP_DIR"

# Headless auth. Suppress stdout so the API key never lands in logs.
hookdeck ci --api-key "$HOOKDECK_API_KEY" --name "dagger-dump" >/dev/null

# If only the human name was supplied, resolve it to a connection ID via the
# server-side --name filter. An exact-name match is required (the filter is a
# prefix match on some Hookdeck endpoints).
if [ -z "$CONNECTION_ID" ]; then
  if [ -z "$CONNECTION_NAME" ]; then
    echo "ERR: pass --connection-id or --connection-name" >&2
    exit 2
  fi
  hookdeck gateway connection list \
    --name "$CONNECTION_NAME" \
    --limit 100 \
    --output json > "$HD_TMP_DIR/list.json"
  matches=$(jq -r --arg n "$CONNECTION_NAME" '.models[] | select(.name == $n) | .id' "$HD_TMP_DIR/list.json")
  match_count=$(printf '%s\n' "$matches" | grep -c . || true)
  if [ "$match_count" -eq 0 ]; then
    echo "ERR: no connection found with name '$CONNECTION_NAME'" >&2
    exit 3
  elif [ "$match_count" -gt 1 ]; then
    echo "ERR: $match_count connections share name '$CONNECTION_NAME'; pass --connection-id instead:" >&2
    jq -r --arg n "$CONNECTION_NAME" '.models[] | select(.name == $n) | "  \(.id)  \(.name)"' "$HD_TMP_DIR/list.json" >&2
    exit 3
  fi
  CONNECTION_ID="$matches"
  echo "[resolved]  '$CONNECTION_NAME' -> $CONNECTION_ID"
fi

# Resolve the connection's human name so the host-side script can rename the
# export dir to something readable. Falls back to the ID if the lookup fails.
if hookdeck gateway connection get "$CONNECTION_ID" --output json > "$HD_TMP_DIR/conn.json" 2>"$HD_TMP_DIR/err"; then
  jq -r '.name // empty' "$HD_TMP_DIR/conn.json" > "$OUT_DIR/.connection_name"
else
  echo "warn: could not resolve connection name: $(cat "$HD_TMP_DIR/err")" >&2
  : > "$OUT_DIR/.connection_name"
fi

next=""
count=0
page=0
while :; do
  page=$((page + 1))
  if [ -z "$next" ]; then
    hookdeck gateway event list \
      --connection-id "$CONNECTION_ID" \
      --limit "$LIMIT" \
      --output json > "$HD_TMP_DIR/page.json"
  else
    hookdeck gateway event list \
      --connection-id "$CONNECTION_ID" \
      --limit "$LIMIT" \
      --next "$next" \
      --output json > "$HD_TMP_DIR/page.json"
  fi

  page_count=$(jq -r '.models | length' "$HD_TMP_DIR/page.json")
  echo "[page $page] $page_count events"

  if [ "$page_count" -eq 0 ]; then
    break
  fi

  # Stream IDs so a giant page doesn't blow out shell argv.
  while IFS= read -r id; do
    [ -z "$id" ] && continue
    hookdeck gateway event get "$id" --output json > "$OUT_DIR/${id}.json"
    # raw-body can legitimately be empty (e.g. GET-style events). Don't fail
    # the whole dump on a single missing body.
    if ! hookdeck gateway event raw-body "$id" > "$OUT_DIR/${id}.body" 2>"$HD_TMP_DIR/err"; then
      echo "  warn: raw-body failed for $id: $(cat "$HD_TMP_DIR/err")" >&2
      rm -f "$OUT_DIR/${id}.body"
    fi
    count=$((count + 1))
    if [ -n "$MAX" ] && [ "$count" -ge "$MAX" ]; then
      echo "Reached --max-events ($MAX). Stopping."
      echo "$count" > "$OUT_DIR/.event_count"
      exit 0
    fi
  done < <(jq -r '.models[].id' "$HD_TMP_DIR/page.json")

  next=$(jq -r '.pagination.next // empty' "$HD_TMP_DIR/page.json")
  if [ -z "$next" ] || [ "$next" = "null" ]; then
    break
  fi
done

echo "Total events written: $count"
echo "$count" > "$OUT_DIR/.event_count"
"""


_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _slugify(name: str) -> str:
    """Filesystem-safe slug derived from a connection's display name."""
    slug = _SLUG_RE.sub("-", name).strip("-._").lower()
    return slug or ""


async def _dump_via_dagger(
    *,
    connection_id: str | None,
    connection_name: str | None,
    output_dir: Path,
    api_key: str,
    limit_per_page: int,
    max_events: int | None,
) -> None:
    """Run the Dagger pipeline that dumps Hookdeck events for one connection.

    Exactly one of `connection_id` / `connection_name` should be provided; the
    container resolves a name to an ID before paginating events. `OUT_DIR`/
    `HD_TMP_DIR` are deliberately left unset here -- DUMP_SCRIPT's own
    `${OUT_DIR:-/out}` / `${HD_TMP_DIR:-/tmp/hd}` defaults already match this
    container's ephemeral filesystem layout.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    async with dagger.connection(dagger.Config(log_output=sys.stderr)):
        # set_secret keeps the key out of container layer history; only visible
        # inside the running exec via the env var binding below.
        api_secret = dagger.dag.set_secret("hookdeck-api-key", api_key)

        container = (
            dagger.dag.container()
            .from_("node:20-alpine")
            .with_exec(["apk", "add", "--no-cache", "bash", "jq", "ca-certificates"])
            .with_exec(["npm", "install", "-g", "hookdeck-cli"])
            .with_new_file("/work/dump.sh", contents=DUMP_SCRIPT, permissions=0o755)
        )

        executed = (
            container.with_secret_variable("HOOKDECK_API_KEY", api_secret)
            .with_env_variable("CONNECTION_ID", connection_id or "")
            .with_env_variable("CONNECTION_NAME", connection_name or "")
            .with_env_variable("LIMIT_PER_PAGE", str(limit_per_page))
            .with_env_variable(
                "MAX_EVENTS",
                str(max_events) if max_events is not None else "",
            )
            .with_exec(["/work/dump.sh"])
        )

        await executed.directory("/out").export(str(output_dir))


# ---------------------------------------------------------------------------
# Flox executor -- the fallback for sandboxes with no Dagger engine.
# ---------------------------------------------------------------------------


# Expected SHA-256 digest of the *extracted* `hookdeck` binary for each
# supported platform -- not the archive's digest. Two reasons to check the
# binary itself, both learned the hard way in review:
#
# 1. Recorded here rather than fetched from GitHub alongside the download.
#    Verifying against a checksum pulled from the same (mutable) release, at
#    the same time, doesn't pin anything -- an altered or compromised release
#    would replace both together and still "verify" clean.
# 2. Verified on every call, including a cache hit (see
#    `_ensure_hookdeck_cli_installed`) -- trusting a cached file purely
#    because it exists at the expected path would let a corrupted or
#    tampered `tmp/hookdeck-dump-bin/<version>/hookdeck` execute silently
#    forever. Checking the binary's own bytes covers both the fresh-install
#    and the cache-hit path with one function.
#
# Recorded by extracting v2.3.1's own published release archives and hashing
# the `hookdeck` file inside each. Update these alongside HOOKDECK_CLI_VERSION
# when bumping the pin.
_HOOKDECK_CLI_BINARY_CHECKSUMS: dict[str, str] = {
    "hookdeck_2.3.1_darwin_arm64.tar.gz": (
        "886c1766750d1bdf55f3d9149cd3743053e9cfe185078e7271377cae149a6c21"
    ),
    "hookdeck_2.3.1_linux_arm64.tar.gz": (
        "d23cac1c02f6ba6aa82982c3645ea7cdfc55f7bff677c35da9876d28a8cfc600"
    ),
    "hookdeck_2.3.1_linux_amd64.tar.gz": (
        "c5ed7a90497bad3a84fd5f68bb8089b236c04bb9f3bb6a4f8fb062f99061d528"
    ),
}


def _hookdeck_release_asset() -> str:
    """Map the host platform to its hookdeck-cli release asset filename.

    Only three combinations matter -- `.flox/env/manifest.toml` restricts
    supported systems to aarch64-darwin, aarch64-linux, and x86_64-linux.
    """
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return f"hookdeck_{HOOKDECK_CLI_VERSION}_darwin_arm64.tar.gz"
    if system == "Linux" and machine in ("arm64", "aarch64"):
        return f"hookdeck_{HOOKDECK_CLI_VERSION}_linux_arm64.tar.gz"
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return f"hookdeck_{HOOKDECK_CLI_VERSION}_linux_amd64.tar.gz"
    msg = (
        f"No pinned hookdeck-cli v{HOOKDECK_CLI_VERSION} release for "
        f"{system}/{machine}. Supported: darwin/arm64, linux/arm64, "
        f"linux/amd64."
    )
    raise RuntimeError(msg)


def _download(url: str) -> bytes:
    """Fetch a fixed, internally-built https:// GitHub-releases URL."""
    with urllib.request.urlopen(url) as resp:  # noqa: S310 # nosec B310 -- fixed https:// GitHub-releases URL
        return resp.read()


def _verify_checksum(data: bytes, asset_name: str) -> None:
    """Verify `data` (the extracted `hookdeck` binary) against this file's
    pinned digest for `asset_name`.

    See `_HOOKDECK_CLI_BINARY_CHECKSUMS` for why the expected digest is a
    committed constant, checked on both install and every cache hit.
    """
    expected = _HOOKDECK_CLI_BINARY_CHECKSUMS.get(asset_name)
    if expected is None:
        msg = f"No pinned checksum recorded for {asset_name}."
        raise RuntimeError(msg)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        msg = f"Checksum mismatch for {asset_name}: expected {expected}, got {actual}."
        raise RuntimeError(msg)


def _ensure_hookdeck_cli_installed(prefix: Path) -> Path:
    """Download, checksum-verify, and atomically install the pinned hookdeck-cli release.

    Re-verifies the cached binary's checksum on every call, including when
    it's already present at `prefix` -- a file existing at the expected path
    is not evidence it's the right, uncorrupted file; a failed re-check
    deletes it and falls through to a fresh install rather than silently
    executing whatever is there. Delete `tmp/hookdeck-dump-bin/<version>/`
    to force a refresh unconditionally (the path is scoped by
    `HOOKDECK_CLI_VERSION`, so bumping the pin can't silently keep serving an
    older cached binary either). No Node/npm involved: the npm package
    hookdeck-cli ships is a thin postinstall wrapper around this same
    prebuilt Go binary (see module docstring).

    Extracts into a private staging directory first and installs the binary
    with `Path.replace` -- an atomic rename on POSIX. Two concurrent
    invocations racing on an empty cache each stage and verify their own
    copy independently rather than sharing one extraction in place, so
    neither can observe (or execute) the other's partially-written file;
    whichever finishes last simply wins the final swap.
    """
    asset_name = _hookdeck_release_asset()
    binary = prefix / "hookdeck"
    if binary.exists():
        try:
            _verify_checksum(binary.read_bytes(), asset_name)
        except RuntimeError:
            binary.unlink()  # corrupted or tampered cache -- fall through and reinstall
        else:
            return prefix

    archive_bytes = _download(f"{HOOKDECK_RELEASE_BASE}/{asset_name}")

    prefix.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(dir=prefix.parent, prefix=".hookdeck-install-"),
    )
    try:
        archive_path = staging_dir / asset_name
        archive_path.write_bytes(archive_bytes)
        with tarfile.open(archive_path) as tar:
            tar.extractall(staging_dir, filter="data")  # noqa: S202 - filter="data" restricts extraction to safe entries; binary content verified below
        staged_binary = staging_dir / "hookdeck"
        _verify_checksum(staged_binary.read_bytes(), asset_name)
        staged_binary.chmod(staged_binary.stat().st_mode | 0o111)
        staged_binary.replace(binary)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    return prefix


def _write_dump_script(work_dir: Path) -> Path:
    """Materialize DUMP_SCRIPT onto the host -- the Flox counterpart of the
    Dagger executor's `with_new_file("/work/dump.sh", ...)`. Same string,
    same permissions, single source of truth for the bash/jq dump logic.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    script_path = work_dir / "dump.sh"
    script_path.write_text(DUMP_SCRIPT)
    script_path.chmod(0o755)
    return script_path


def _dump_via_flox(
    *,
    connection_id: str | None,
    connection_name: str | None,
    output_dir: Path,
    api_key: str,
    limit_per_page: int,
    max_events: int | None,
) -> None:
    """Run DUMP_SCRIPT via a Flox-activated host shell instead of Dagger.

    Writes directly into `output_dir` (via `OUT_DIR`) -- no `/out` +
    `.directory().export()` indirection needed since there's no container
    boundary to cross.

    Uses a fresh `tempfile.mkdtemp` work dir per call rather than a fixed
    `tmp/hookdeck-dump-work` path: two concurrent invocations sharing one
    directory could otherwise delete each other's in-flight dump script and
    paging scratch files. The dir is removed in `finally` regardless of
    outcome. `HOOKDECK_CONFIG_FILE` is pointed at a file inside that same
    scratch dir so `hookdeck ci`'s auth state -- which, unlike the Dagger
    container, would otherwise persist to the operator's real
    `~/.config/hookdeck/config.toml` -- never lands outside `tmp/` and is
    removed with everything else.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    bin_dir = _ensure_hookdeck_cli_installed(FLOX_HOOKDECK_BIN_PREFIX)

    (REPO_ROOT / "tmp").mkdir(parents=True, exist_ok=True)
    work_dir = Path(
        tempfile.mkdtemp(dir=REPO_ROOT / "tmp", prefix="hookdeck-dump-work-"),
    )
    try:
        script_path = _write_dump_script(work_dir)
        hd_tmp_dir = work_dir / "hd"

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["HOOKDECK_API_KEY"] = api_key
        env["HOOKDECK_CONFIG_FILE"] = str(work_dir / "hookdeck-config.toml")
        env["CONNECTION_ID"] = connection_id or ""
        env["CONNECTION_NAME"] = connection_name or ""
        env["LIMIT_PER_PAGE"] = str(limit_per_page)
        env["MAX_EVENTS"] = str(max_events) if max_events is not None else ""
        env["OUT_DIR"] = str(output_dir)
        env["HD_TMP_DIR"] = str(hd_tmp_dir)

        subprocess.run(  # noqa: S603 — argv list, shell disabled
            [*flox_activate_prefix(REPO_ROOT), "bash", str(script_path)],
            cwd=REPO_ROOT,
            env=env,
            check=True,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _use_flox() -> bool:
    """Whether `GTM_HOOKDECK_DUMP_VIA_FLOX` selects the Flox executor.

    Routed through `env_flag` so an unrecognized value (e.g. a typo) fails
    loudly instead of silently selecting Dagger.
    """
    return env_flag("GTM_HOOKDECK_DUMP_VIA_FLOX")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--connection-id",
        help="Hookdeck connection ID (e.g. web_xxx).",
    )
    target.add_argument(
        "--connection-name",
        help=(
            "Hookdeck connection display name (e.g. rb2b-visits-mock). "
            "Resolved to an ID inside the container. Must be an exact, "
            "unambiguous match."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=(
            f"Root output directory. Events land in <output-dir>/<connection-name>/ "
            f"(falls back to the connection ID if the name lookup fails). "
            f"Default: {DEFAULT_OUTPUT_ROOT}"
        ),
    )
    parser.add_argument(
        "--limit-per-page",
        type=int,
        default=100,
        help="Page size passed to `event list --limit` (default: 100).",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="Stop after N events. Default: dump all.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("HOOKDECK_API_KEY")
    if not api_key:
        print(
            "HOOKDECK_API_KEY is not set. Run via:\n"
            '  infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=<dev|prod> -- scripts/hookdeck-connection_events-dump.py ...',
            file=sys.stderr,
        )
        return 2

    # Export into a staging dir first; the container writes the resolved
    # connection name to /out/.connection_name during the dump, and we then
    # rename the staging dir to <slug>/ on the host. Staging exists because we
    # don't know the human-readable name until after the container runs (and
    # the user may have supplied just a name, with no ID yet).
    target_token = args.connection_id or args.connection_name or ""
    root = args.output_dir.resolve()
    staging_dir = root / f".staging-{_slugify(target_token) or 'dump'}"
    if args.connection_id:
        print(f"[connection]  id={args.connection_id}")
    else:
        print(f"[connection]  name={args.connection_name}")
    print(f"[limit/page]  {args.limit_per_page}")
    if args.max_events is not None:
        print(f"[max events]  {args.max_events}")

    if staging_dir.exists():
        shutil.rmtree(staging_dir)

    if _use_flox():
        _dump_via_flox(
            connection_id=args.connection_id,
            connection_name=args.connection_name,
            output_dir=staging_dir,
            api_key=api_key,
            limit_per_page=args.limit_per_page,
            max_events=args.max_events,
        )
    else:
        asyncio.run(
            _dump_via_dagger(
                connection_id=args.connection_id,
                connection_name=args.connection_name,
                output_dir=staging_dir,
                api_key=api_key,
                limit_per_page=args.limit_per_page,
                max_events=args.max_events,
            ),
        )

    name_file = staging_dir / ".connection_name"
    raw_name = name_file.read_text().strip() if name_file.exists() else ""
    slug = _slugify(raw_name) if raw_name else _slugify(target_token)
    final_dir = root / slug

    if final_dir.exists():
        shutil.rmtree(final_dir)
    staging_dir.rename(final_dir)
    print(f"[output]      {final_dir}")

    count_file = final_dir / ".event_count"
    if count_file.exists():
        print(f"[done]        wrote {count_file.read_text().strip()} events")
    else:
        print("[done]        dump complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
