#!/usr/bin/env python3
r"""Find pre-fix synthetic duplicate Attio meetings for manual UI deletion (ai-4bz.9).

Before the dedup fix landed (ai-4bz.8), the live cal.com webhook and a partial
backfill minted ``api-token`` Meeting records that shadow the real
calendar-synced ``system`` meetings at the same slot. They CANNOT be deleted via
the API: Attio's ``meetings`` object is a beta, GET-only surface (no DELETE /
PATCH / PUT), and ``meetings`` is not a standard object so
``DELETE /v2/objects/meetings/records/{id}`` 404s. Removal is manual in the Attio
UI - this script just produces the list the operator deletes from.

This is a READ-ONLY scan. It streams every meeting in a date range, pairs each
api-token meeting with the same-slot system meeting it duplicates, and writes
the generated orphan reports. ``src.attio.orphan_meetings.write_orphan_csvs``
and this command's generated help are the authoritative descriptions of their
names and confidence classifications.

Meetings exist only in the PROD Attio workspace (the feature is unprovisioned in
dev), so run this against ``--env=prod``.

Usage:
    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \\
        --env=prod -- scripts/attio-find-orphan-meetings.py
    infisical run ... -- scripts/attio-find-orphan-meetings.py \\
        --start 2023-09-01 --end 2026-09-01
    infisical run ... -- scripts/attio-find-orphan-meetings.py --limit 200

Requires ATTIO_API_KEY in the environment - inject via Infisical.
"""

from __future__ import annotations

import pathlib as _pathlib
import sys as _sys

_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
from scripts.lib.uv_bootstrap import bootstrap_uv as _bootstrap_uv  # noqa: E402

if __name__ == "__main__":
    _bootstrap_uv(script_path=__file__, mode="python")

import datetime as dt
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click
import typer

if TYPE_CHECKING:
    from collections.abc import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
# scripts/ resolves via the editable install's packages.find, but bootstrap the
# repo root onto sys.path too so the entrypoint runs even when the package
# metadata isn't picked up (matches the other scripts/ entrypoints).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.edge import iter_meetings_in_range  # noqa: E402
from src.attio.orphan_meetings import (  # noqa: E402
    classify,
    detect_orphans,
    write_orphan_csvs,
)

DEFAULT_OUTPUT_DIR = REPO_ROOT / "out" / "orphan-cleanup"

# The cal.com booking era - the range the prior orphan scans used. Bounds the
# server-side query so we page ~12.5k rows instead of the whole workspace.
DEFAULT_START = "2023-09-01"
DEFAULT_END = "2026-09-01"
_DATE_ERROR = "must be an ISO 8601 date or datetime"

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=__doc__,
)


def _parse_date(value: str, *, end_of_day: bool = False) -> dt.datetime:
    """Parse an ISO date/datetime to a tz-aware UTC bound.

    A date-only ``--end`` (no time component) is widened to end-of-day so the
    documented inclusive ``[start, end]`` range actually covers meetings later on
    that day - parsing ``2026-09-01`` as midnight would silently drop them. A
    date-only ``--start`` correctly stays at midnight (inclusive lower bound). An
    explicit time is always respected.
    """
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    if end_of_day and "T" not in value and " " not in value:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def _run(
    start: str,
    end: str,
    output_dir: Path,
    limit: int | None,
    allow_non_prod: bool,  # noqa: FBT001
) -> int:
    try:
        parsed_start = _parse_date(start)
    except ValueError as exc:
        raise typer.BadParameter(
            _DATE_ERROR,
            param_hint="--start",
        ) from exc
    try:
        parsed_end = _parse_date(end, end_of_day=True)
    except ValueError as exc:
        raise typer.BadParameter(
            _DATE_ERROR,
            param_hint="--end",
        ) from exc

    print("# Attio orphan-meeting scan (READ-ONLY)")
    print(f"# range {parsed_start.date()} .. {parsed_end.date()}")

    # Confirm the token is active and name the workspace before a long page loop -
    # an inactive token or the wrong workspace (meetings live only in prod) is the
    # likely failure, and surfacing it up front beats a confusing empty result.
    from src.edge import fetch_token_scopes

    active, scopes, workspace = fetch_token_scopes()
    if not active:
        print(
            "ERROR: Attio token is inactive (GET /v2/self active=false). "
            "Regenerate it in Attio - Settings - Developers and update Infisical.",
            file=sys.stderr,
        )
        return 1
    # GET /v2/meetings needs a meeting-read scope. Attio grants scopes at either
    # ``:read`` or ``:read-write`` granularity, so accept EITHER - asserting one
    # exact string would falsely reject a token that holds the other (the prod
    # token carries ``meeting:read-write``). Fail fast here rather than deep in
    # the pagination loop with an opaque 4xx.
    if not ({"meeting:read", "meeting:read-write"} & scopes):
        print(
            "ERROR: Attio token lacks a meeting-read scope "
            "(need 'meeting:read' or 'meeting:read-write'). "
            f"Present scopes: {sorted(scopes)}. Regenerate the token in Attio - "
            "Settings - Developers with meeting read access.",
            file=sys.stderr,
        )
        return 1
    print(f"# workspace={workspace!r}")
    if "dlthub" not in workspace.lower():
        if not allow_non_prod:
            print(
                f"ERROR: workspace {workspace!r} is not the prod workspace "
                "(dlthub). Meetings are only provisioned in prod, so this scan "
                "would silently report 'no orphans'. Re-run with --env=prod, or "
                "pass --allow-non-prod to override.",
                file=sys.stderr,
            )
            return 1
        print(
            f"WARNING: scanning non-prod workspace {workspace!r} "
            "(--allow-non-prod set); expect an empty result.",
            file=sys.stderr,
        )

    meetings = []
    system_count = api_created_count = other_count = 0
    for candidate in iter_meetings_in_range(start=parsed_start, end=parsed_end):
        # Check the cap BEFORE consuming so --limit N scans exactly N meetings.
        if limit is not None and len(meetings) >= limit:
            break
        meetings.append(candidate)
        if candidate.created_by_type == "system":
            system_count += 1
        elif candidate.created_by_type == "api-token":
            api_created_count += 1
        else:
            other_count += 1

    rows = detect_orphans(meetings)
    confident, review = classify(rows)
    paths = write_orphan_csvs(rows, output_dir)

    print(
        f"# scanned={len(meetings)} system={system_count} "
        f"api-token={api_created_count} other={other_count}",
    )
    print(f"# confident={len(confident)} review={len(review)}")
    print(f"# confident -> {paths['confident']}")
    print(f"# review    -> {paths['review']}")
    print(f"# all       -> {paths['all']}")
    print(
        "# NEXT: delete the orphans_confident.csv rows by hand in the Attio UI "
        "(no DELETE API). Judge orphans_review.csv individually.",
    )
    return 0


@app.command()
def cli(
    start: str = typer.Option(
        DEFAULT_START,
        "--start",
        help="ISO 8601 lower bound on meeting start.",
    ),
    end: str = typer.Option(
        DEFAULT_END,
        "--end",
        help="ISO 8601 upper bound on meeting start.",
    ),
    output_dir: Path = typer.Option(
        DEFAULT_OUTPUT_DIR,
        "--output-dir",
        help="Directory for the orphan CSVs.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Stop after scanning this many meetings (positive client-side cap for a small test run).",
    ),
    allow_non_prod: bool = typer.Option(  # noqa: FBT001
        False,  # noqa: FBT003
        "--allow-non-prod",
        help="Permit running against a non-prod Attio workspace.",
    ),
) -> int:
    if limit is not None and limit < 1:
        msg = "must be a positive integer"
        raise typer.BadParameter(msg, param_hint="--limit")  # noqa: TRY003
    return _run(
        start=start,
        end=end,
        output_dir=output_dir,
        limit=limit,
        allow_non_prod=allow_non_prod,
    )


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
