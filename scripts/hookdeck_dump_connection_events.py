"""Dump all Hookdeck events attached to a single connection.

Runs the `hookdeck` CLI inside a Dagger-managed container so the dump is
reproducible and the host machine does not need npm / hookdeck CLI installed.
Authenticates to Hookdeck headlessly via `hookdeck ci --api-key ...`, paginates
`event list --connection-id`, and writes one `<event-id>.json` (metadata) plus
one `<event-id>.body` (raw request body) per event to the host output dir.

You can identify the connection either by ID (`--connection-id web_xxx`) or by
its human name (`--connection-name rb2b-visits-mock`). Name lookups happen
inside the container so the host still needs no Hookdeck install.

Usage:
    infisical run -- uv run python scripts/hookdeck_dump_connection_events.py \\
        --connection-id web_xxx

    infisical run -- uv run python scripts/hookdeck_dump_connection_events.py \\
        --connection-name rb2b-visits-mock

    infisical run -- uv run python scripts/hookdeck_dump_connection_events.py \\
        --connection-id web_xxx --max-events 50

Requires `HOOKDECK_API_KEY` in the environment (inject via Infisical).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

import dagger

# Anchor on the script's directory so relative output paths resolve correctly
# regardless of the CWD `uv run` was invoked from.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "out" / "hookdeck-events"

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

mkdir -p /out
mkdir -p /tmp/hd

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
    --output json > /tmp/hd/list.json
  matches=$(jq -r --arg n "$CONNECTION_NAME" '.models[] | select(.name == $n) | .id' /tmp/hd/list.json)
  match_count=$(printf '%s\n' "$matches" | grep -c . || true)
  if [ "$match_count" -eq 0 ]; then
    echo "ERR: no connection found with name '$CONNECTION_NAME'" >&2
    exit 3
  elif [ "$match_count" -gt 1 ]; then
    echo "ERR: $match_count connections share name '$CONNECTION_NAME'; pass --connection-id instead:" >&2
    jq -r --arg n "$CONNECTION_NAME" '.models[] | select(.name == $n) | "  \(.id)  \(.name)"' /tmp/hd/list.json >&2
    exit 3
  fi
  CONNECTION_ID="$matches"
  echo "[resolved]  '$CONNECTION_NAME' -> $CONNECTION_ID"
fi

# Resolve the connection's human name so the host-side script can rename the
# export dir to something readable. Falls back to the ID if the lookup fails.
if hookdeck gateway connection get "$CONNECTION_ID" --output json > /tmp/hd/conn.json 2>/tmp/hd/err; then
  jq -r '.name // empty' /tmp/hd/conn.json > /out/.connection_name
else
  echo "warn: could not resolve connection name: $(cat /tmp/hd/err)" >&2
  : > /out/.connection_name
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
      --output json > /tmp/hd/page.json
  else
    hookdeck gateway event list \
      --connection-id "$CONNECTION_ID" \
      --limit "$LIMIT" \
      --next "$next" \
      --output json > /tmp/hd/page.json
  fi

  page_count=$(jq -r '.models | length' /tmp/hd/page.json)
  echo "[page $page] $page_count events"

  if [ "$page_count" -eq 0 ]; then
    break
  fi

  # Stream IDs so a giant page doesn't blow out shell argv.
  while IFS= read -r id; do
    [ -z "$id" ] && continue
    hookdeck gateway event get "$id" --output json > "/out/${id}.json"
    # raw-body can legitimately be empty (e.g. GET-style events). Don't fail
    # the whole dump on a single missing body.
    if ! hookdeck gateway event raw-body "$id" > "/out/${id}.body" 2>/tmp/hd/err; then
      echo "  warn: raw-body failed for $id: $(cat /tmp/hd/err)" >&2
      rm -f "/out/${id}.body"
    fi
    count=$((count + 1))
    if [ -n "$MAX" ] && [ "$count" -ge "$MAX" ]; then
      echo "Reached --max-events ($MAX). Stopping."
      echo "$count" > /out/.event_count
      exit 0
    fi
  done < <(jq -r '.models[].id' /tmp/hd/page.json)

  next=$(jq -r '.pagination.next // empty' /tmp/hd/page.json)
  if [ -z "$next" ] || [ "$next" = "null" ]; then
    break
  fi
done

echo "Total events written: $count"
echo "$count" > /out/.event_count
"""


_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _slugify(name: str) -> str:
    """Filesystem-safe slug derived from a connection's display name."""
    slug = _SLUG_RE.sub("-", name).strip("-._").lower()
    return slug or ""


async def dump_events(
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
    container resolves a name to an ID before paginating events.
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
            "  infisical run -- uv run python scripts/hookdeck_dump_connection_events.py ...",
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

    asyncio.run(
        dump_events(
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
