#!/usr/bin/env -S uv run python
"""Backfill Attio Meeting records (+ a Fireflies summary note) from the personal
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
        --env=dev -- scripts/fireflies-backfill-attio-meetings.py
    infisical run ... -- scripts/fireflies-backfill-attio-meetings.py --execute
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from libs.fireflies import from_motherduck_row
from scripts.lib.env import clean_env, infisical_run_example, parse_dotenv
from src.fireflies import DATABASE, iter_assembled_rows, to_attio_operations

if TYPE_CHECKING:
    from src.attio.ops import AttioOp

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
TMP_DIR = REPO_ROOT / "tmp"
_TOKEN_ENV = "MOTHERDUCK_TOKEN"  # nosec B105 -- env var name, not a credential


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill Attio Meeting records from the MotherDuck "
        "fireflies-backfill database.",
        epilog="Example:\n  "
        + infisical_run_example("scripts/fireflies-backfill-attio-meetings.py"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write to Attio. Default is a dry run that only prints the planned ops.",
    )
    parser.add_argument(
        "--no-notes",
        action="store_true",
        help="Upsert Meetings only; skip the Fireflies summary note.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after this many transcripts. Useful for a small test run.",
    )
    return parser


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


def main() -> int:
    args = _build_parser().parse_args()
    _ensure_motherduck_token()

    lines: list[str] = []

    def emit(msg: str) -> None:
        print(msg)
        lines.append(msg)

    mode = "EXECUTE" if args.execute else "DRY RUN"
    emit(f"# Fireflies → Attio meeting backfill ({mode})")

    from libs.motherduck import connect
    from src.attio.export import execute

    if args.execute:
        # Fail fast on a misconfigured Attio token instead of failing every row
        # deep inside a write (ai-ica).
        from libs.attio.preflight import assert_attio_token_scopes

        assert_attio_token_scopes()

    con = connect(DATABASE)

    processed = written = failed = 0
    fail_details: list[str] = []
    # Dedup observability (ai-av8): tally how *successful* meeting ops resolved
    # so the smoke test can prove participant-matching worked.
    #  - ``matched_existing``: collapsed onto a pre-existing calendar-synced /
    #    Fathom / Cal.com meeting via ``match_existing_by_participants`` (action
    #    "noop", no new row) — this is the dedup signal we care about.
    #  - ``via_find_or_create``: no participant match, so it went through
    #    ``find_or_create_meeting`` on the synthetic ical_uid. The Attio layer
    #    reports action "created" for this path whether the row was freshly
    #    inserted OR returned existing-by-ical_uid (a replay), so we do NOT try
    #    to split fresh-vs-replay here — it cannot be told apart from the action.
    # Failed meeting ops are excluded (they show up in the ``failed`` summary).
    meetings_matched = meetings_via_find_or_create = 0

    for raw in iter_assembled_rows(con):
        if args.limit is not None and processed >= args.limit:
            break
        processed += 1
        rec_id = raw.get("id", "?")
        try:
            recording = from_motherduck_row(raw)
            ops: list[AttioOp] = to_attio_operations(
                recording,
                include_notes=not args.no_notes,
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

        if not args.execute:
            continue

        result = execute(ops)
        # Surface what each op actually did — record_id, action, and the
        # matched_existing flag — instead of discarding execute()'s outcomes.
        # This is the empirical dedup proof for the smoke test (ai-av8): a
        # Fireflies meeting that coincides with an existing meeting must report
        # action=noop matched_existing=True, not action=created.
        for outcome in result.outcomes:
            matched = bool(outcome.envelope.meta.get("matched_existing"))
            emit(
                f"    -> {outcome.op_type} action={outcome.envelope.action} "
                f"matched_existing={matched} record_id={outcome.record_id}",
            )
            # OpOutcome.op_type is the op class name (type(op).__name__), e.g.
            # "UpsertMeeting" — not the snake_case AttioOp.op_type.
            if outcome.op_type == "UpsertMeeting":
                if matched:
                    meetings_matched += 1
                elif outcome.envelope.action != "failed":
                    # Non-matched success → the find_or_create path.
                    meetings_via_find_or_create += 1
        if result.success:
            written += 1
        else:
            failed += 1
            # Surface the failing op's actual error envelope, not just the
            # opaque fail_reason ("op_failed"), so failures are triageable from
            # the report alone.
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
        + (f"written={written} " if args.execute else "")
        + f"failed={failed}",
    )
    if args.execute:
        # The dedup payoff: matched_existing meetings collapsed onto a
        # pre-existing row (no duplicate); via_find_or_create went through the
        # synthetic-ical_uid path (no participant match found).
        emit(
            f"- meetings: matched_existing={meetings_matched} "
            f"via_find_or_create={meetings_via_find_or_create}",
        )
    for detail in fail_details:
        emit(f"- FAIL {detail}")
    if not args.execute:
        emit("\n(dry run — pass --execute to write to Attio)")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
    report = TMP_DIR / f"fireflies-backfill-{stamp}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport written to {report}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
