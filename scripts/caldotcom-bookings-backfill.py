#!/usr/bin/env -S uv run python
"""Backfill historical cal.com bookings into Attio by replaying them through the
live Modal webhook (``export-to-attio-from-calcom-bookings``).

API-only: the cal.com REST API is the authoritative source (there are no source
CSVs). Each booking is fetched, deduped, status-filtered, wrapped in the same
``Webhook`` envelope a live ``BOOKING_CREATED`` event carries, and POSTed
one-by-one to the deployed Modal endpoint — the exact path live events take, so
the backfill can't drift from production dispatch behavior.

Idempotency is handler-side and free: the webhook collapses each replay onto
``canonical_meeting_uid(host_email, start)`` → Attio ``external_id``, so re-runs
PATCH/no-op rather than duplicate. That makes this script safe to re-run, which
is also the recovery path — failed POSTs land in ``failures.jsonl`` and are
re-fed simply by running again.

Two distinct "status" concepts (easy to conflate):
  * ``--lifecycle`` → the collection endpoint's ``status`` *query* param, a
    lifecycle bucket (upcoming/recurring/past/cancelled/unconfirmed). Selects
    which bookings the API returns.
  * ``--status`` → the NORMALIZED Attio RSVP value
    (accepted/pending/declined/tentative) the webhook derives from the booking's
    raw status. Selects which fetched records we actually replay, using the
    webhook's own normalization (cancelled/rejected → declined, unknown/missing
    → accepted) so a narrowed filter matches what production would ingest.
    Defaults to ``all`` to mirror the live webhook, which does NOT drop by this
    field (only MEETING_STARTED/PING are always dropped). Narrow with e.g.
    ``--status accepted`` when you only want confirmed meetings.

Usage (dry-run is the default; nothing is POSTed until --apply):
    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \
      --env=dev -- scripts/caldotcom-bookings-backfill.py --dry-run
    infisical run ... -- scripts/caldotcom-bookings-backfill.py --apply --limit 5
    infisical run ... -- scripts/caldotcom-bookings-backfill.py --apply

Requires ``CALCOM_API_KEY`` (cal.com fetch) and, for ``--apply``, the Modal
workspace prefix (defaults to ``devx``; override with ``MODAL_WORKSPACE``).

Meetings land in PROD only (ai-h5y). Attio's ``/v2/meetings`` feature is ALPHA
and is provisioned only in the prod Attio workspace, so ``UpsertMeeting`` lands
a Meeting record only when the target webhook authenticates against PROD Attio.
The dev/prod split is the injected ``ATTIO_API_KEY`` (the Infisical env the
Modal app was deployed under) — NOT the ``MODAL_WORKSPACE`` URL prefix. Against
a dev-backed webhook every ``UpsertMeeting`` dead-letters with ``not_found``
(meetings unprovisioned); that is EXPECTED, not a backfill gap, and is correctly
non-retryable (see ``_RETRYABLE_BODY_ERROR_CODES``). Run the backfill against the
prod-backed webhook deployment to actually create meeting records.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Anchor on the script's own directory so paths resolve regardless of CWD (per
# repo CLAUDE.md path-anchoring rule). `uv run path/to/script.py` does NOT chdir.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cli.webhook._modal import modal_url_for_app  # noqa: E402
from libs.caldotcom.client import CalcomClient  # noqa: E402
from libs.caldotcom.models import BookingCreatedPayload, Webhook  # noqa: E402
from scripts.lib.env import infisical_run_example  # noqa: E402

# Intentionally reuse the webhook's exact status normalization rather than
# duplicate the mapping — keeping the backfill's filter in lockstep with live.
from src.caldotcom.webhook.booking import (  # noqa: E402
    _caldotcom_status_to_attio,  # pyright: ignore[reportPrivateUsage]
)

# The deployed Modal app the live cal.com webhook runs as. Matches
# ``src/caldotcom/webhook/booking.py::attio_get_app_name``.
WEBHOOK_APP_NAME = "export-to-attio-from-calcom-bookings"

OUT_DIR = REPO_ROOT / "out" / "caldotcom-backfill"

# Allowed filter vocabularies, validated up front so a typo fails fast instead
# of silently narrowing the replay to zero/partial while still exiting 0.
# --lifecycle = the API 'status' query buckets; --status = normalized Attio RSVP
# values (see _caldotcom_status_to_attio). Empty --lifecycle / --status all mean
# "no filter".
_LIFECYCLE_VOCAB = frozenset(
    {"upcoming", "recurring", "past", "cancelled", "unconfirmed"},
)
_STATUS_VOCAB = frozenset({"accepted", "pending", "declined", "tentative"})

# Bounded exponential backoff. 4 attempts, ~1s/2s/4s between — retry only
# transient failures (5xx + network/timeout), never 4xx (a 4xx means the
# payload is bad and retrying won't help; it's logged to failures.jsonl).
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0


def _envelope_for(booking: BookingCreatedPayload) -> dict[str, Any]:
    """Wrap a fetched booking in the live ``BOOKING_CREATED`` webhook envelope.

    Near-identity: the API ``BookingOutput`` already validated as a
    ``BookingCreatedPayload``, so dumping it reproduces what a live webhook
    delivers. ``createdAt`` uses the booking's own creation time when present
    (preserved via ``extra="allow"``), falling back to ``start``.
    """
    payload = booking.model_dump(mode="json")
    created_at = payload.get("createdAt") or payload.get("start")
    return {
        "triggerEvent": "BOOKING_CREATED",
        "createdAt": created_at,
        "payload": payload,
    }


def _passes_live_gate(booking: BookingCreatedPayload) -> bool:
    """Mirror the webhook's BOOKING_CREATED gate (``_validation_result``).

    A record that fails this would be dropped by the live handler, so we skip
    it here too rather than POST a payload that can't land.
    """
    return (
        bool(booking.uid) and bool(booking.attendees) and bool(booking.creator_email())
    )


def _write_jsonl(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    append: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")


# Attio error codes that are transient (the downstream service was briefly
# unreachable) and worth retrying when they surface inside a 200 body. Mirrors
# libs/attio/errors.py — only connectivity is genuinely retryable; auth/schema/
# not-found/validation are deterministic and must dead-letter immediately.
# Note: against a dev-backed webhook, UpsertMeeting dead-letters here with
# `not_found` because Attio's meetings feature is unprovisioned in dev (ai-h5y).
# That is expected and correctly non-retryable — run against prod to land meetings.
_RETRYABLE_BODY_ERROR_CODES = ("connectivity_error",)


def _body_failure(response: httpx.Response) -> str | None:
    """Inspect a 2xx webhook body for application-level failure.

    The Modal handler returns HTTP 200 even when processing fails, in two
    shapes: a JSON envelope with a ``success`` flag + per-op ``outcomes`` (the
    normal dispatch result), or a PLAIN reason string when it rejects an invalid
    webhook (``export_to_attio._export``). Checking only the status code, or
    assuming JSON, would silently report either failure as sent.

    Returns ``None`` only when the body affirmatively reports success; any other
    body (op failure, plain rejection string, unexpected/non-JSON shape) is
    surfaced as a failure summary. Whether the caller retries is decided
    separately by ``_is_retryable_body`` — transient codes (connectivity) retry,
    deterministic ones (404, permission, validation) dead-letter to
    ``failures.jsonl`` (re-runnable, since the replay is idempotent).
    """
    raw_text = response.text
    try:
        body: Any = response.json()
    except ValueError:
        # Non-JSON 2xx is unexpected from this endpoint. An empty body is the
        # only benign case; anything else is a failure we must not hide.
        stripped = raw_text.strip()
        return f"non-JSON 2xx body: {stripped[:300]}" if stripped else None
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            # The handler returns a bare reason string when it rejects an
            # invalid webhook — a failure, not a silent success.
            return f"webhook rejected (non-JSON body): {body[:300]}"
    if not isinstance(body, dict):
        return f"webhook returned unexpected body type: {type(body).__name__}"
    if body.get("success") is True:
        return None
    failed = [
        f"{o.get('op_type')}={o.get('error') or o.get('errors')}"
        for o in body.get("outcomes", [])
        if isinstance(o, dict) and o.get("success") is False
    ]
    return (
        f"webhook success=false: {'; '.join(failed)[:600]}"
        if failed
        else (f"webhook success=false: {json.dumps(body)[:600]}")
    )


def _is_retryable_body(failure_summary: str) -> bool:
    """Whether a ``_body_failure`` summary names a transient (retryable) code."""
    return any(code in failure_summary for code in _RETRYABLE_BODY_ERROR_CODES)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds form) if present and sane."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        secs = float(raw)
    except ValueError:
        # HTTP-date form is also legal but rare from JSON APIs; skip it and let
        # the caller fall back to exponential backoff rather than misparse.
        return None
    return secs if secs >= 0 else None


def _post_with_retry(
    client: httpx.Client,
    url: str,
    envelope: dict[str, Any],
) -> tuple[int | None, str | None]:
    """POST one envelope. Returns (status_code, error). Retries transients only.

    Two failure layers: the HTTP status AND the application-level ``success``
    flag in a 200 body (see ``_body_failure``). Retried as transient: 5xx,
    network/timeout, ``429 Too Many Requests`` (honoring ``Retry-After``), and a
    200 body whose failure names a transient code (``connectivity_error`` — see
    ``_is_retryable_body``). Other 4xx and deterministic body failures (404,
    permission, validation) route straight to failures.jsonl.
    """
    last_error: str | None = None
    retry_after: float | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        retry_after = None
        try:
            response = client.post(url, json=envelope)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_error = f"transport: {exc!r}"
        else:
            code = response.status_code
            if code < 400:
                body_err = _body_failure(response)
                if body_err is None:
                    return code, None
                if not _is_retryable_body(body_err):
                    # Deterministic application failure — dead-letter, no retry.
                    return code, body_err
                last_error = body_err  # transient downstream — fall through
            elif code != 429 and code < 500:
                # Deterministic client rejection — do not retry. 429 is the one
                # 4xx that's transient, so it falls through to the retry path.
                return code, f"http {code}: {response.text[:500]}"
            else:
                last_error = f"http {code}: {response.text[:500]}"
                if code == 429:
                    retry_after = _retry_after_seconds(response)
        if attempt < MAX_ATTEMPTS:
            backoff = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            time.sleep(retry_after if retry_after is not None else backoff)
    return None, last_error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Default. Fetch, filter, and write envelopes to out/ without POSTing.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="POST envelopes to the live Modal webhook (idempotent; safe to re-run).",
    )
    parser.add_argument(
        "--lifecycle",
        default="",
        help="Comma list for the API 'status' query param "
        "(upcoming/recurring/past/cancelled/unconfirmed). Default: empty = no "
        "filter = ALL lifecycle buckets, for a complete historical replay "
        "consistent with --status all. Narrow with e.g. --lifecycle past,upcoming.",
    )
    parser.add_argument(
        "--status",
        default="all",
        help="Comma list of NORMALIZED Attio RSVP values to replay "
        "(accepted/pending/declined/tentative), or 'all'. Matches the webhook's "
        "own normalization (cancelled/rejected → declined, unknown/missing → "
        "accepted), so narrowing here selects exactly what production would "
        "ingest. Default: all — replay every booking, mirroring the live webhook.",
    )
    parser.add_argument(
        "--after-start",
        default=None,
        help="ISO-8601 afterStart filter.",
    )
    parser.add_argument("--before-end", default=None, help="ISO-8601 beforeEnd filter.")
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="API take/page size.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of records replayed (smoke test). Applies after filtering.",
    )
    args = parser.parse_args()

    if args.page_size < 1:
        parser.error("--page-size must be >= 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")

    apply = bool(args.apply)

    lifecycle = [s.strip().lower() for s in args.lifecycle.split(",") if s.strip()]
    unknown_lifecycle = sorted(set(lifecycle) - _LIFECYCLE_VOCAB)
    if unknown_lifecycle:
        parser.error(
            f"--lifecycle has unknown bucket(s) {unknown_lifecycle}; "
            f"allowed: {sorted(_LIFECYCLE_VOCAB)} (or empty = all buckets).",
        )

    status_arg = args.status.strip().lower()
    allowed_statuses: set[str] | None
    if status_arg == "all":
        allowed_statuses = None
    else:
        allowed_statuses = {
            s.strip().lower() for s in args.status.split(",") if s.strip()
        }
        unknown_status = sorted(allowed_statuses - _STATUS_VOCAB)
        if unknown_status:
            parser.error(
                f"--status has unknown value(s) {unknown_status}; "
                f"allowed: {sorted(_STATUS_VOCAB)} or 'all'.",
            )

    # ---- Fetch ----
    with CalcomClient.from_env() as client:
        bookings = client.list_bookings(
            lifecycle_status=lifecycle or None,
            after_start=args.after_start,
            before_end=args.before_end,
            page_size=args.page_size,
        )
    print(f"[backfill] fetched {len(bookings)} bookings (lifecycle={lifecycle})")

    # ---- Dedup on the stable booking key (uid), keeping the last seen ----
    by_uid: dict[str, BookingCreatedPayload] = {}
    for b in bookings:
        by_uid[b.uid] = b
    deduped = list(by_uid.values())
    if len(deduped) != len(bookings):
        print(f"[backfill] deduped {len(bookings)} → {len(deduped)} unique uids")

    # ---- Filter: RSVP status + live gate ----
    kept: list[BookingCreatedPayload] = []
    skipped_status = 0
    skipped_gate: list[str] = []
    for b in deduped:
        # Filter on the SAME normalized Attio RSVP value the live webhook
        # computes (cancelled/rejected → declined, unknown/missing → accepted),
        # so a narrowed --status matches exactly what production would ingest —
        # e.g. a booking with an unfamiliar status string still counts as
        # "accepted" here, just as the handler treats it.
        normalized = _caldotcom_status_to_attio(b.status)
        if allowed_statuses is not None and normalized not in allowed_statuses:
            skipped_status += 1
            continue
        if not _passes_live_gate(b):
            skipped_gate.append(b.uid)
            continue
        kept.append(b)

    if skipped_status:
        print(
            f"[backfill] skipped {skipped_status} by RSVP status filter ({status_arg})",
        )
    if skipped_gate:
        print(
            f"[backfill] skipped {len(skipped_gate)} failing the live gate "
            f"(uid/attendees/host-email): {skipped_gate[:10]}"
            f"{' …' if len(skipped_gate) > 10 else ''}",
        )

    if args.limit is not None:
        kept = kept[: args.limit]
        print(f"[backfill] limited to {len(kept)} records")

    # ---- Map to envelopes + validate locally before any POST ----
    envelopes: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    for b in kept:
        envelope = _envelope_for(b)
        try:
            Webhook.model_validate(envelope)
        except Exception as exc:  # noqa: BLE001 - surface bad records, keep going
            invalid_rows.append(
                {
                    "uid": b.uid,
                    "error": f"local_validation: {exc!r}",
                    "envelope": envelope,
                },
            )
            continue
        envelopes.append(envelope)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _write_jsonl(OUT_DIR / "bookings.jsonl", envelopes)
    print(f"[backfill] wrote {len(envelopes)} envelopes → {OUT_DIR / 'bookings.jsonl'}")

    # A record that can't even be built into a valid Webhook is a real backfill
    # gap, not a no-op: persist it to the dead-letter file and let it drive a
    # non-zero exit (even on a dry run) so a silent partial backfill can't
    # masquerade as success.
    if invalid_rows:
        _write_jsonl(OUT_DIR / "failures.jsonl", invalid_rows, append=True)
        print(
            f"[backfill] {len(invalid_rows)} records failed local validation → "
            f"{OUT_DIR / 'failures.jsonl'} (NOT sent): "
            f"{[r['uid'] for r in invalid_rows[:5]]}",
        )

    if not apply:
        print(
            "[backfill] DRY RUN — nothing POSTed. Re-run with --apply to send.\n"
            f"           example: {infisical_run_example('scripts/caldotcom-bookings-backfill.py', extra_args='--apply')}",
        )
        return 1 if invalid_rows else 0

    # ---- Send: one-by-one POST with retry/backoff + per-record logging ----
    # Outcomes are persisted INCREMENTALLY: each record's result row (and any
    # failure) is appended and flushed the moment its POST resolves, rather than
    # buffered in memory until the loop ends. This gives live progress (tail
    # results.jsonl, or watch the stdout N/total counter) and makes partial
    # progress durable if the sweep is interrupted — the replay is idempotent, so
    # re-running simply no-ops the rows that already landed. The in-memory lists
    # are retained only for the final summary counts.
    url = modal_url_for_app(WEBHOOK_APP_NAME)
    total = len(envelopes)
    print(f"[backfill] POSTing {total} envelopes → {url}", flush=True)
    results_path = OUT_DIR / "results.jsonl"
    failures_path = OUT_DIR / "failures.jsonl"
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    sent_ok = 0
    with httpx.Client(timeout=30.0) as http:
        for index, envelope in enumerate(envelopes, start=1):
            uid = envelope["payload"].get("uid")
            code, error = _post_with_retry(http, url, envelope)
            row = {
                "uid": uid,
                "status_code": code,
                "action": "sent" if error is None else "failed",
                "error": error,
            }
            results.append(row)
            if error is None:
                sent_ok += 1
            else:
                failure_row = {"uid": uid, "error": error, "envelope": envelope}
                failures.append(failure_row)
                # Persist the dead-letter BEFORE the result row: failures.jsonl is
                # the recovery-critical artifact (re-fed on the next run), so if the
                # process is interrupted between the two appends the worst case is a
                # dead-letter with no matching result row — harmless, since re-feed
                # is idempotent. The reverse (a "failed" result row whose dead-letter
                # was lost) would be a silent backfill gap, so never order it that way.
                _write_jsonl(failures_path, [failure_row], append=True)
            _write_jsonl(results_path, [row], append=True)
            print(
                f"[backfill] {index}/{total} {row['action']} (http {code}) uid={uid}",
                flush=True,
            )

    print(
        f"[backfill] done: {sent_ok}/{len(envelopes)} sent, {len(failures)} failed.\n"
        f"           results → {OUT_DIR / 'results.jsonl'}",
    )
    total_failed = len(failures) + len(invalid_rows)
    if total_failed:
        print(
            f"           {total_failed} failures "
            f"({len(invalid_rows)} local-validation, {len(failures)} send) → "
            f"{OUT_DIR / 'failures.jsonl'} (re-run the script to retry; replay is idempotent)",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
