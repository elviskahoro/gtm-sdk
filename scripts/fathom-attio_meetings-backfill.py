#!/usr/bin/env -S uv run python
"""Backfill Attio Meeting records (+ Fathom summary / action-item notes) from the
Fathom REST API.

Motivation (ai-t58): pre-plan-02 Cal.com reschedules left phantom duplicate
Meeting rows that cannot be deleted, and some real meetings never landed in
Attio. Fathom only records meetings that *actually happened*, at their real
start time, so listing recordings and upserting them through the existing
webhook → Attio transform backfills the genuine records and naturally avoids
recreating rescheduled-away slots. It does NOT delete the pre-existing
duplicates (Attio /v2/meetings has no DELETE).

This reuses the live webhook's transform (``src/fathom/webhook/call.py``) and op
dispatcher (``src/attio/export.py``) verbatim — no forked mapping logic.

Idempotency: ``UpsertMeeting`` is keyed on the canonical ical_uid (find-or-create)
and ``UpsertNote`` is deduped by (title, meeting_id) on the parent Person in
``src/attio/export.py`` — the note hangs off a participant and is associated to
the meeting via ``meeting_id`` (meetings cannot be a note parent; see ai-gez), so
re-running is safe (no duplicate meetings or notes). This also makes recovery
trivial when a transient Fathom 5xx aborts a run mid-pagination: just re-run —
already-written records become no-ops.

Limitation: Attio's /v2/meetings exposes only GET and POST (find-or-create) —
no PATCH/PUT. find-or-create returns an EXISTING row unchanged, so a meeting
already present in Attio will NOT pick up later description/metadata changes
from a re-run. New recordings are created complete; pre-existing rows are
effectively frozen. Run the backfill only once hydration is final (see ai-crf).

Default is a DRY RUN. Pass --execute to write to Attio. NOTE: a dry run only
prints the planned ops — it never calls ``src.attio.export.execute``, so it
CANNOT surface failures that only happen at write time. In particular, a note
whose parent attendee is not yet a Person in Attio fails with ``unresolved_ref``
at --execute (the meeting's own POST normally auto-creates participants, but a
pre-existing meeting is a no-op and creates none). A clean dry run is therefore
NOT proof the --execute run will be failure-free.

Usage:
    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \\
        --env=dev -- scripts/fathom-attio_meetings-backfill.py
    infisical run ... -- scripts/fathom-attio_meetings-backfill.py --execute
    infisical run ... -- scripts/fathom-attio_meetings-backfill.py \\
        --created-after 2026-01-01T00:00:00Z --recorded-by martin@dlthub.com

Requires FATHOM_API_KEY (always) and ATTIO_API_KEY (for --execute) in the
environment — inject via Infisical.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
import typer

from libs.fathom import iter_meetings, webhook_from_sdk_meeting

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
TMP_DIR = REPO_ROOT / "tmp"


def _ops_for_meeting(meeting: Any, *, include_notes: bool) -> list[Any]:
    from src.attio.ops import UpsertMeeting
    from src.fathom.webhook.call import Webhook as CallWebhook

    mapped = webhook_from_sdk_meeting(meeting)
    call = CallWebhook.model_validate(mapped.model_dump(mode="json"))
    ops = call.attio_get_operations()
    if not include_notes:
        ops = [op for op in ops if isinstance(op, UpsertMeeting)]
    return ops


def _describe_op(op: Any) -> str:
    from src.attio.ops import UpsertMeeting, UpsertNote

    if isinstance(op, UpsertMeeting):
        return (
            f"upsert_meeting ical_uid={op.external_ref.ical_uid} "
            f"start={op.start} title={op.title!r}"
        )
    if isinstance(op, UpsertNote):
        meeting = op.meeting.model_dump() if op.meeting else None
        return (
            f"upsert_note title={op.title!r} parent={op.parent.model_dump()} "
            f"meeting={meeting}"
        )
    return f"{op.op_type} {op.model_dump()}"


def main(
    *,
    created_after: str | None = None,
    created_before: str | None = None,
    recorded_by: list[str] | None = None,
    execute: bool = False,
    no_notes: bool = False,
    limit: int | None = None,
) -> int:

    lines: list[str] = []

    def emit(msg: str) -> None:
        print(msg)
        lines.append(msg)

    mode = "EXECUTE" if execute else "DRY RUN"
    emit(f"# Fathom → Attio meeting backfill ({mode})")

    from src.attio.export import execute as execute_operations

    if execute:
        # Validate the Attio token's scopes once up front so a misconfigured key
        # fails the whole run immediately with an actionable message, rather than
        # failing every row deep inside a write (ai-ica).
        from libs.attio.preflight import assert_attio_token_scopes

        assert_attio_token_scopes()

    processed = written = failed = 0
    fail_details: list[str] = []

    for meeting in iter_meetings(
        created_after=created_after,
        created_before=created_before,
        recorded_by=recorded_by,
        # Always fetch the summary: it populates the meeting description, not
        # just the separate summary note. --no-notes only drops the UpsertNote
        # ops (filtered in _ops_for_meeting), not the description body.
        include_summary=True,
        include_action_items=not no_notes,
    ):
        if limit is not None and processed >= limit:
            break
        processed += 1
        try:
            ops = _ops_for_meeting(meeting, include_notes=not no_notes)
        except Exception as exc:  # noqa: BLE001 — one bad recording must not abort the run
            failed += 1
            detail = (
                f"recording_id={getattr(meeting, 'recording_id', '?')} map_error={exc}"
            )
            fail_details.append(detail)
            emit(f"- SKIP {detail}")
            continue

        emit(f"- recording_id={meeting.recording_id} ({len(ops)} ops)")
        for op in ops:
            emit(f"    - {_describe_op(op)}")

        if not execute:
            continue

        result = execute_operations(ops)
        if result.success:
            written += 1
        else:
            failed += 1
            fail_details.append(
                f"recording_id={meeting.recording_id} fail_index={result.fail_index} "
                f"reason={result.fail_reason}",
            )

    emit("")
    emit(
        f"## Summary: processed={processed} "
        + (f"written={written} " if execute else "")
        + f"failed={failed}",
    )
    for detail in fail_details:
        emit(f"- FAIL {detail}")
    if not execute:
        emit("\n(dry run — pass --execute to write to Attio)")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
    report = TMP_DIR / f"fathom-backfill-{stamp}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport written to {report}")

    return 1 if failed else 0


def _cli(
    created_after: str | None = typer.Option(None, "--created-after"),
    created_before: str | None = typer.Option(None, "--created-before"),
    recorded_by: list[str] | None = typer.Option(None, "--recorded-by"),
    execute: bool = typer.Option(False, "--execute"),
    no_notes: bool = typer.Option(False, "--no-notes"),
    limit: int | None = typer.Option(None, "--limit"),
) -> None:
    raise typer.Exit(
        main(
            created_after=created_after,
            created_before=created_before,
            recorded_by=recorded_by,
            execute=execute,
            no_notes=no_notes,
            limit=limit,
        )
    )


if __name__ == "__main__":
    typer.run(_cli)
