#!/usr/bin/env python3
r"""Backfill Attio Meeting records (+ a Fireflies summary note) from the personal
MotherDuck ``fireflies-backfill`` database.

Motivation: we switched meeting recording from Fireflies → Fathom. The
historical Fireflies recordings were only ever exported (via dlt) into a
personal MotherDuck database and never landed in Attio. This script reads those
transcripts and upserts them through the *same* op vocabulary + dispatcher the
live Fathom pipeline uses (``src/attio/export.py``), so there is no forked
Fireflies → Attio mapping.

Idempotency: ``UpsertMeeting`` is keyed on the canonical ical_uid
(``canonical_meeting_uid(host_email, start)`` — find-or-create) and ``UpsertNote``
is deduped by (title, meeting_id) on the parent Person in ``src/attio/export.py``.
So re-running is safe (no duplicate meetings or notes), and a Fireflies meeting
that shares host+start-minute with a Fathom/Cal.com record collapses onto that
single Attio meeting.

Limitation: Attio's /v2/meetings exposes only GET and POST (find-or-create) — no
PATCH/PUT. A meeting already present in Attio will NOT pick up later
description/metadata changes from a re-run; pre-existing rows are effectively
frozen. New transcripts are created complete.

Default is a DRY RUN. Pass --execute to write to Attio.

Auth:
- ``MOTHERDUCK_TOKEN`` — a *personal* token, kept in the repo-root ``.env.local``
  (intentionally not in Infisical). The script self-loads it from there if it is
  not already in the environment, so no ``set -a; source .env.local`` is needed.
- ``ATTIO_API_KEY`` — required only for --execute; inject via Infisical.

Usage:
    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \\
        --env=dev -- scripts/fireflies-attio_meetings-backfill.py
    infisical run ... -- scripts/fireflies-attio_meetings-backfill.py --execute
"""

from __future__ import annotations

import pathlib as _pathlib
import sys as _sys

_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
from scripts.lib.uv_bootstrap import bootstrap_uv as _bootstrap_uv  # noqa: E402

if __name__ == "__main__":
    _bootstrap_uv(script_path=__file__, mode="python")

import datetime as dt
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import typer

from libs.fireflies import from_motherduck_row
from scripts.lib.env import clean_env, parse_dotenv
from src.fireflies import DATABASE, iter_assembled_rows, to_attio_operations

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.attio.ops import AttioOp

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
TMP_DIR = REPO_ROOT / "tmp"
_TOKEN_ENV = "MOTHERDUCK_TOKEN"  # nosec B105 -- env var name, not a credential

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

app = typer.Typer(add_completion=False, help=__doc__)


def _ensure_motherduck_token() -> None:
    """Load MOTHERDUCK_TOKEN from REPO_ROOT/.env.local if absent from the env.

    Avoids requiring `set -a; source .env.local` (per repo guidance) by parsing
    the single key ourselves. No-op when it is already set.
    """
    if clean_env(os.environ.get(_TOKEN_ENV)):
        return
    env_file = REPO_ROOT / ".env.local"
    if not env_file.is_file():
        return
    value = clean_env(parse_dotenv(env_file.read_text()).get(_TOKEN_ENV))
    if value:
        os.environ[_TOKEN_ENV] = value


def _describe_op(op: Any) -> str:
    from src.attio.ops import UpsertMeeting, UpsertNote

    if isinstance(op, UpsertMeeting):
        return (
            f"upsert_meeting ical_uid={op.external_ref.ical_uid} "
            f"start={op.start} title={op.title!r} "
            f"participants={len(op.participants)} links={len(op.linked_records)}"
        )
    if isinstance(op, UpsertNote):
        meeting = op.meeting.model_dump() if op.meeting else None
        return (
            f"upsert_note title={op.title!r} parent={op.parent.model_dump()} "
            f"meeting={meeting}"
        )
    return f"{op.op_type} {op.model_dump()}"


def _run(
    *,
    execute: bool,  # noqa: FBT001,FBT002
    no_notes: bool,  # noqa: FBT001,FBT002
    limit: int | None,
) -> int:
    _ensure_motherduck_token()

    lines: list[str] = []

    def emit(msg: str) -> None:
        typer.echo(msg)
        lines.append(msg)

    mode = "EXECUTE" if execute else "DRY RUN"
    emit(f"# Fireflies → Attio meeting backfill ({mode})")

    from libs.motherduck import connect
    from src.attio.export import execute as execute_ops

    if execute:
        from libs.attio.preflight import assert_attio_token_scopes

        assert_attio_token_scopes()

    con = connect(DATABASE)

    processed = written = failed = 0
    fail_details: list[str] = []
    meetings_matched = meetings_via_find_or_create = 0

    for raw in iter_assembled_rows(con):
        if limit is not None and processed >= limit:
            break
        processed += 1
        rec_id = raw.get("id", "?")
        try:
            recording = from_motherduck_row(raw)
            ops: list[AttioOp] = to_attio_operations(
                recording,
                include_notes=not no_notes,
            )
        except Exception as exc:  # noqa: BLE001 — one bad row must not abort the run
            failed += 1
            detail = f"transcript_id={rec_id} map_error={exc}"
            fail_details.append(detail)
            emit(f"- SKIP {detail}")
            continue

        emit(f"- transcript_id={rec_id} ({len(ops)} ops)")
        for op in ops:
            emit(f"    - {_describe_op(op)}")

        if not execute:
            continue

        result = execute_ops(ops)
        for outcome in result.outcomes:
            matched = bool(outcome.envelope.meta.get("matched_existing"))
            emit(
                f"    -> {outcome.op_type} action={outcome.envelope.action} "
                f"matched_existing={matched} record_id={outcome.record_id}",
            )
            if outcome.op_type == "UpsertMeeting":
                if matched:
                    meetings_matched += 1
                elif outcome.envelope.action != "failed":
                    meetings_via_find_or_create += 1
        if result.success:
            written += 1
        else:
            failed += 1
            err_detail = ""
            if result.fail_index is not None and result.fail_index < len(
                result.outcomes,
            ):
                env = result.outcomes[result.fail_index].envelope
                if env.errors:
                    err_detail = " errors=" + "; ".join(
                        str(e.model_dump()) for e in env.errors
                    )
            fail_details.append(
                f"transcript_id={rec_id} fail_index={result.fail_index} "
                f"reason={result.fail_reason}{err_detail}",
            )

    emit("")
    emit(
        f"## Summary: processed={processed} "
        + (f"written={written} " if execute else "")
        + f"failed={failed}",
    )
    if execute:
        emit(
            f"- meetings: matched_existing={meetings_matched} "
            f"via_find_or_create={meetings_via_find_or_create}",
        )
    for detail in fail_details:
        emit(f"- FAIL {detail}")
    if not execute:
        emit("\n(dry run — pass --execute to write to Attio)")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
    report = TMP_DIR / f"fireflies-backfill-{stamp}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    typer.echo(f"\nReport written to {report}")

    return 1 if failed else 0


@app.command(help=__doc__)
def backfill(
    execute: bool = typer.Option(  # noqa: FBT001,FBT002
        False,  # noqa: FBT003
        "--execute",
        help="Write to Attio. Default is a dry run that only prints the planned ops.",
    ),
    no_notes: bool = typer.Option(  # noqa: FBT001,FBT002
        False,  # noqa: FBT003
        "--no-notes",
        help="Upsert Meetings only; skip the Fireflies summary note.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Stop after this many transcripts. Useful for a small test run.",
    ),
) -> int:
    return _run(execute=execute, no_notes=no_notes, limit=limit)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = app(
            args=list(argv) if argv is not None else None,
            standalone_mode=False,
        )
    except click.ClickException as exc:
        exc.show()
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
