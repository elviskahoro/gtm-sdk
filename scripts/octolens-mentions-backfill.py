#!/usr/bin/env -S uv run python
"""Backfill historical Octolens dlt/dlthub mentions into Attio.

Two phases in one script:

1. **build** — collect the real dlt/dlthub mentions (see
   ``src/octolens/backfill.py::include_mention``), dedup by URL, and write one
   ``mentions.jsonl``. A review table (``url | source | keyword | reason``) is
   printed so the content-signal list can be audited and tuned. The build has
   two sources, selected with ``--source``:
     - ``csv`` (default) — read the local Octolens CSV exports. No score in the
       data, so every mention is stamped ``relevance=unknown``.
     - ``api`` — pull *all-time* dlt/dlthub mentions from the live v2 REST API
       (``POST /api/v2/mentions``). By default it walks the **entire** org feed
       and narrows with ``include_mention`` for full CSV-recall parity (catches
       content/URL-only matches). ``--keyword-filtered`` (or naming a
       ``--keyword``/``--keyword-id``) is a faster opt-in that filters server-side
       by the brand keywords. Carries a **real** relevance verdict
       (``relevanceScore`` 0=high/1=medium/2=low). Requires ``OCTOLENS_API_KEY``.
2. **send** — map each JSONL row to the Octolens webhook payload, validate it
   locally against the real ``Webhook`` model, and POST it one-by-one to the
   deployed Attio Modal endpoint. Resumable via a per-out-dir ``sent.log``.

**Idempotent** end to end: the send phase skips URLs already in ``sent.log``, and
the Attio webhook upserts by ``source_id`` (UpsertMention/UpsertPerson), so a
re-run never duplicates. The no-downgrade rule in ``libs/attio/mentions.py``
preserves a higher live relevance if a re-send carries a lower/unknown score.

**csv source needs no secrets** — the build is local and the send POSTs the
*public* Modal endpoint (which holds its own ATTIO_API_KEY). Point it at the CSV
dir with ``--data-dir`` or ``OCTOLENS_DATA_DIR`` (the exports live in the parent
``ai/`` repo, so there is no portable default):

    export OCTOLENS_DATA_DIR=~/Documents/ai/data/octolens
    ./scripts/octolens-mentions-backfill.py                 # build + preview
    ./scripts/octolens-mentions-backfill.py --send          # dry-run send
    ./scripts/octolens-mentions-backfill.py --send --apply  # real POST (dev first!)

**api source needs ``OCTOLENS_API_KEY``** (read scope; mint in Octolens
Settings → API). Inject it via Infisical — use a separate ``--out-dir`` so the
api and csv ``sent.log``s don't mix:

    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \
      --env=<dev|prod> -- ./scripts/octolens-mentions-backfill.py \
      --source api --rebuild --out-dir out/octolens-backfill-api
    # then dry-run, then --apply (the send phase re-uses the same build):
    ... --source api --send --out-dir out/octolens-backfill-api
    ... --source api --send --apply --out-dir out/octolens-backfill-api

A plain ``--source api`` build/preview (no ``--send``) always re-fetches live —
the cached JSONL is reused only by the send phase, so the API path never
silently serves stale data. ``--rebuild`` forces a fresh pull in any case.

dev vs prod for the *send* is selected by ``--endpoint-url`` (or the
``MODAL_WORKSPACE`` env var that ``modal_url_for_app`` reads). Confirm which
endpoint targets dev Attio before a full ``--apply`` run.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, get_args

# Anchor on the script's directory so paths resolve regardless of the CWD
# `uv run` was invoked from, and so local `src`/`cli`/`libs` imports work.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
import orjson  # noqa: E402

from cli.webhook._modal import modal_url_for_app  # noqa: E402
from libs.octolens import Source  # noqa: E402
from libs.octolens.client import OctolensClient  # noqa: E402
from scripts.lib.env import infisical_run_example  # noqa: E402
from src.octolens.backfill import (  # noqa: E402
    api_mention_to_row,
    build_webhook_payload,
    include_mention,
)
from src.octolens.webhook import Webhook  # noqa: E402

# Source platforms the inbound-webhook Mention model accepts. The v2 API spans a
# superset (medium, stackoverflow, producthunt, tiktok, ...); api-source rows
# outside this set are skipped with a logged count — the webhook couldn't ingest
# them either.
_SUPPORTED_SOURCES: frozenset[str] = frozenset(get_args(Source))

# Some mention bodies exceed Python's default 128 KiB CSV field cap.
csv.field_size_limit(10**9)

# The legacy export has an opaque, non-conforming schema — never load it.
LEGACY_FILES = frozenset({"octolens-mentions-202603082207.csv"})
# The CSV exports live in the parent `ai/` repo (outside this repo), so there is
# no portable default. Read the dir from OCTOLENS_DATA_DIR, else require
# --data-dir; main() fails fast with a clear message when neither is set.
_ENV_DATA_DIR = os.environ.get("OCTOLENS_DATA_DIR")
DEFAULT_DATA_DIR = Path(_ENV_DATA_DIR) if _ENV_DATA_DIR else None
DEFAULT_OUT_DIR = REPO_ROOT / "out" / "octolens-backfill"


# Sidecar marker recording the FULL build fingerprint (source + every input that
# changes the included set) that produced a mentions.jsonl, so a rerun with
# different params — a different source, keyword filter, relevance window, or
# page cap — can't silently reuse a stale cache in the same out-dir.
_BUILD_MARKER = ".build-fingerprint"


def _reuse_cached_jsonl(
    jsonl_path: Path,
    *,
    fingerprint: str,
    rebuild: bool,
) -> Path | None:
    """Return the cached JSONL to reuse, or None to (re)build.

    ``--rebuild`` always regenerates. Otherwise an existing JSONL is reused only
    when its sibling fingerprint marker matches ``fingerprint`` exactly — any
    difference (source, keyword filter, ``--include-low``, ``--page-size``,
    ``--max-pages``) is refused loudly rather than silently serving stale data.

    A **missing** marker triggers a rebuild (returns None), never a reuse. This
    covers both a legacy pre-marker out-dir and a build interrupted between
    committing the JSONL and stamping the marker — in either case the on-disk
    data's provenance is unknown, so rebuilding (re-reading CSVs / re-fetching
    the API) is the only safe choice. The marker is always written last, so its
    presence certifies the JSONL was committed under that exact fingerprint.
    """
    if rebuild or not jsonl_path.exists():
        return None
    marker = jsonl_path.parent / _BUILD_MARKER
    if not marker.exists():
        # Provenance unknown (legacy dir or interrupted build) — rebuild rather
        # than guess. Never reuse markerless data as if it matched.
        return None
    prior = marker.read_text().strip()
    if prior != fingerprint:
        raise SystemExit(
            f"[build] {jsonl_path} was built with a different config\n"
            f"  cached:    {prior}\n"
            f"  requested: {fingerprint}\n"
            "Pass --rebuild to regenerate it, or use a different --out-dir.",
        )
    print(f"[build] reusing {jsonl_path} (pass --rebuild to regenerate)")
    return jsonl_path


def build_jsonl(data_dir: Path, out_dir: Path, *, rebuild: bool) -> Path:
    """Phase 1: filter + dedup the CSV exports into a single JSONL."""
    files = sorted(
        p
        for p in data_dir.glob("octolens-mentions-*.csv")
        if p.name not in LEGACY_FILES
    )
    if not files:
        raise SystemExit(f"[build] no Octolens CSVs found under {data_dir}")

    # Fingerprint the resolved data dir + the exact input set (name + nanosecond
    # mtime), so adding/removing/replacing an export — or pointing at a different
    # --data-dir — invalidates a cached JSONL instead of silently reusing stale
    # output. st_mtime_ns (not whole seconds) catches a same-second replacement.
    # Globbed + stat'd only (no full read), so the reuse check stays cheap.
    fingerprint = (
        "csv|"
        + str(data_dir.resolve())
        + "|"
        + ",".join(f"{p.name}:{p.stat().st_mtime_ns}" for p in files)
    )
    jsonl_path = out_dir / "mentions.jsonl"
    cached = _reuse_cached_jsonl(jsonl_path, fingerprint=fingerprint, rebuild=rebuild)
    if cached is not None:
        return cached

    # Dedup by URL FIRST — the newest export row wins on a collision (files are
    # sorted by their timestamped name), regardless of whether it passes the
    # keyword filter — so a newer export always supersedes an older row for the
    # same URL. Only then apply the inclusion predicate, to the deduped (latest)
    # row. (Filtering before dedup could keep a stale older row when the newest
    # export no longer matches.) The first header carries a UTF-8 BOM ("﻿URL"),
    # so we MUST read with utf-8-sig or the URL dedup key silently misses.
    latest: dict[str, dict[str, Any]] = {}
    read = 0
    for path in files:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                read += 1
                url = (row.get("URL") or "").strip()
                if url:
                    row["_source_file"] = path.name
                    latest[url] = row

    print(f"[build] files={[p.name for p in files]}")
    seen = _filter_and_write(latest, jsonl_path, fingerprint=fingerprint)
    print(f"[build] read={read} unique_mentions={len(seen)} -> {jsonl_path}")
    return jsonl_path


def _filter_and_write(
    rows: dict[str, dict[str, Any]],
    jsonl_path: Path,
    *,
    fingerprint: str,
) -> list[dict[str, Any]]:
    """Apply ``include_mention`` to already-deduped rows, write the JSONL, report.

    Shared by the CSV (:func:`build_jsonl`) and API
    (:func:`build_jsonl_from_api`) build paths so both produce an identical
    ``mentions.jsonl`` the send phase consumes. Callers dedup ``rows`` under
    whatever key is right for their source (URL for CSV, ``source_id`` for the
    API), so this does NOT re-dedup — re-keying here would re-collapse distinct
    API mentions that share a URL. Writes the build ``fingerprint`` alongside the
    JSONL so a later run with different params can't reuse it. Returns the kept
    rows.
    """
    kept: list[dict[str, Any]] = []
    for row in rows.values():
        keep, reason = include_mention(row)
        if keep:
            row["_include_reason"] = reason
            kept.append(row)

    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically so the JSONL and its fingerprint can never diverge if the
    # process is interrupted mid-write:
    #   1. drop the stale marker — during the rebuild there is NO valid marker,
    #      so a crash leaves a missing marker (treated as legacy "csv": an api
    #      request is refused, a csv request reuses an equivalent csv dataset),
    #      never a new dataset masquerading under an old fingerprint;
    #   2. write the JSONL to a temp file and os.replace() it into place (atomic
    #      on the same filesystem) so a reader never sees a partial file;
    #   3. stamp the new fingerprint last, only once the JSONL is committed.
    marker_path = jsonl_path.parent / _BUILD_MARKER
    marker_path.unlink(missing_ok=True)
    tmp_path = jsonl_path.with_name(jsonl_path.name + ".tmp")
    reasons: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    relevance: Counter[str] = Counter()
    with tmp_path.open("wb") as out:
        for row in kept:
            out.write(orjson.dumps(row))
            out.write(b"\n")
            reasons[row["_include_reason"]] += 1
            sources[(row.get("Source") or "").strip().lower()] += 1
            relevance[str(row.get("_relevance_score") or "unknown")] += 1
    os.replace(tmp_path, jsonl_path)
    marker_path.write_text(fingerprint + "\n")

    print(f"[build] by reason: {dict(reasons)}")
    print(f"[build] by source: {dict(sources.most_common())}")
    print(f"[build] by relevance: {dict(relevance.most_common())}")
    _print_review_table(kept)
    return kept


def _resolve_keyword_ids(
    client: OctolensClient,
    keyword_texts: list[str],
) -> list[int]:
    """Resolve keyword texts (e.g. ``dlt``/``dlthub``) to the org's numeric ids.

    The v2 mentions ``keyword`` filter takes numeric ids, which differ per org,
    so we look them up live via ``GET /api/v2/keywords`` rather than hard-coding.
    Fails loudly if a requested keyword isn't tracked — a silent miss would
    quietly under-pull the backfill.
    """
    wanted = {text.strip().lower() for text in keyword_texts if text.strip()}
    by_text = {
        (kw.get("keyword") or "").strip().lower(): kw.get("id")
        for kw in client.list_keywords()
    }
    resolved: list[int] = []
    missing: list[str] = []
    for text in sorted(wanted):
        kid = by_text.get(text)
        if isinstance(kid, int):
            resolved.append(kid)
        else:
            missing.append(text)
    if missing:
        raise SystemExit(
            f"[build] keyword(s) {missing} not tracked by this org; "
            f"available: {sorted(by_text)}",
        )
    print(f"[build] keyword filter: {sorted(wanted)} -> ids {sorted(resolved)}")
    return resolved


def build_jsonl_from_api(
    out_dir: Path,
    *,
    rebuild: bool,
    include_low: bool,
    page_size: int,
    max_pages: int | None,
    keyword_texts: list[str],
    keyword_ids: list[int] | None,
    all_mentions: bool,
    allow_reuse: bool,
) -> Path:
    """Phase 1 (API source): pull dlt/dlthub mentions from the v2 API into a JSONL.

    Two recall modes, both finishing with ``include_mention`` client-side:

    - **default** (``all_mentions=True``): fetch the **entire** org feed (no
      server-side keyword filter) and let ``include_mention`` decide, exactly
      matching the CSV flow's recall — it catches mentions surfaced by a
      title/body/URL signal even when Octolens never tagged them with the
      ``dlt``/``dlthub`` keyword. Higher volume (walks the whole feed), but it is
      the correct default for "all the mentions we've ever had".
    - **keyword-filtered** (``all_mentions=False``, via ``--keyword-filtered`` or
      an explicit ``--keyword``/``--keyword-id``): filter server-side by the
      brand keyword ids (from ``keyword_ids``, or resolved from ``keyword_texts``
      via :func:`_resolve_keyword_ids`). Much faster — Octolens multi-tags a post
      with every matched keyword, so any post that names the brand carries the
      tag and is caught — but it misses content/URL-only matches. An explicit
      optimization, not the default.

    Each mention is mapped to the CSV-shaped row, deduped by ``source_id``, then
    run through ``include_mention`` (which strips the ``dlt``-keyword noise either
    mode admits). ``include_low`` toggles the API's ``includeAll`` (low-relevance
    is dropped by the webhook regardless, so the default excludes it). Requires
    ``OCTOLENS_API_KEY`` (inject via Infisical).

    Cache reuse is **opt-in** via ``allow_reuse`` (set only in the send phase, so
    the build→dry-run→apply handoff doesn't re-fetch). A plain build/preview
    always hits the live API — the API source's whole value is freshness, so it
    must never silently serve a stale JSONL. Reuse does NOT require credentials:
    replaying a cached build only POSTs the public webhook, so the key is fetched
    lazily, only on the path that actually calls the API.
    """
    # Fingerprint every input that changes the included set, so a rerun with a
    # different keyword filter / relevance window / page cap won't reuse a stale
    # cache. Computed from the raw args (pre-fetch) so the reuse decision needs
    # no API call or credentials. NOTE: the default path keys on the requested
    # keyword *text*, not the live-resolved ids — the cache can't detect the org
    # renaming/re-id'ing a keyword between build and send (a rare, minutes-scale
    # window). Pass --rebuild if the org's keyword catalog changed. Keyword ids
    # take precedence over texts when given.
    if all_mentions:
        kw_part = "all"
    elif keyword_ids:
        kw_part = f"ids={sorted(keyword_ids)}"
    else:
        kw_part = f"texts={sorted(t.strip().lower() for t in keyword_texts)}"
    fingerprint = (
        f"api|{kw_part}|low={int(include_low)}|page={page_size}|max={max_pages}"
    )

    jsonl_path = out_dir / "mentions.jsonl"
    if allow_reuse:
        cached = _reuse_cached_jsonl(
            jsonl_path,
            fingerprint=fingerprint,
            rebuild=rebuild,
        )
        if cached is not None:
            return cached

    # Only the live-fetch path needs the API key — resolve it lazily so a cached
    # replay above never requires OCTOLENS_API_KEY.
    try:
        client = OctolensClient.from_env()
    except RuntimeError as exc:
        example = infisical_run_example(
            "./scripts/octolens-mentions-backfill.py",
            extra_args="--source api --rebuild --out-dir out/octolens-backfill-api",
        )
        raise SystemExit(f"{exc}\n\n  {example}") from exc

    latest: dict[str, dict[str, Any]] = {}
    fetched = 0
    unsupported = 0
    incomplete = 0
    with client:
        if all_mentions:
            print(
                "[build] exhaustive mode: walking the FULL org feed (no keyword "
                "filter) for CSV-recall parity; include_mention narrows "
                "client-side. This is higher-volume — pass --keyword-filtered "
                "for a fast brand-keyword-only pull.",
            )
            filters: dict[str, Any] | None = None
        elif keyword_ids:
            print(f"[build] keyword filter: explicit ids {sorted(keyword_ids)}")
            filters = {"keyword": keyword_ids}
        else:
            filters = {"keyword": _resolve_keyword_ids(client, keyword_texts)}
        for mention in client.list_mentions(
            filters=filters,
            include_all=include_low,
            page_size=page_size,
            max_pages=max_pages,
        ):
            fetched += 1
            row = api_mention_to_row(mention)
            url = (row.get("URL") or "").strip()
            if not url or not (row.get("Source ID") or "").strip():
                # Missing/null identity field (url or source_id) — counted, not
                # silently dropped, so an undercount is visible in the summary.
                incomplete += 1
                continue
            if row.get("Source") not in _SUPPORTED_SOURCES:
                unsupported += 1
                continue
            # Dedup on the stable (source, source_id) key — NOT the URL: distinct
            # mentions can share a URL (e.g. multiple reddit comments on one post)
            # and collapsing them by URL would lose data. API returns newest-first
            # so the first row per key wins.
            dedup_key = f"{row.get('Source')}|{(row.get('Source ID') or '').strip()}"
            latest.setdefault(dedup_key, row)
            if fetched % 500 == 0:
                print(
                    f"[build] fetched={fetched} unique={len(latest)} "
                    f"unsupported_source={unsupported}",
                )

    print(
        f"[build] source=api include_low={include_low} fetched={fetched} "
        f"incomplete={incomplete} unsupported_source={unsupported} "
        f"unique={len(latest)}",
    )
    seen = _filter_and_write(latest, jsonl_path, fingerprint=fingerprint)
    print(f"[build] included={len(seen)} -> {jsonl_path}")
    return jsonl_path


def _print_review_table(rows) -> None:
    """Print `reason | source | keyword | url` so the signal list is auditable."""
    print("\n[review] included mentions (tune src/octolens/backfill.py::DLT_SIGNALS):")
    print(f"  {'reason':<15} {'source':<10} {'keyword':<22} url")
    for row in rows:
        reason = row.get("_include_reason", "")
        source = (row.get("Source") or "").strip().lower()
        keyword = (row.get("Keyword") or "").strip()
        url = (row.get("URL") or "").strip()
        print(f"  {reason:<15} {source:<10} {keyword[:22]:<22} {url[:90]}")


def _load_rows(jsonl_path: Path) -> list[dict[str, Any]]:
    return [
        orjson.loads(line)
        for line in jsonl_path.read_bytes().splitlines()
        if line.strip()
    ]


def _parse_envelope(resp: httpx.Response) -> dict[str, Any] | None:
    """Decode the operation envelope from a 2xx response.

    The endpoint returns ``execute(plan).body()`` as a JSON-*encoded* string
    (the HTTP body is a quoted string), or a plain reason string when the
    webhook was filtered/invalid. Returns the envelope dict, or None when the
    body is a non-envelope reason string.
    """
    try:
        parsed: Any = resp.json()
    except Exception:  # noqa: BLE001 — fall back to raw text
        parsed = resp.text
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, dict) and "success" in parsed:
        return parsed
    return None


def _deliver(
    client: httpx.Client,
    url: str,
    body: bytes,
    *,
    retries: int = 3,
) -> tuple[str, str]:
    """POST one mention and classify the result as ``(status, detail)``.

    status:
      - ``"ok"``      — envelope ``success == true`` (record written).
      - ``"failed"``  — HTTP 4xx/5xx after retries, OR envelope
                        ``success == false`` (e.g. a failed Attio op).
                        **HTTP 200 alone is NOT success**: this endpoint returns
                        200 with ``success=false`` when an operation errors.
      - ``"skipped"`` — endpoint returned a plain reason string (filtered or
                        invalid webhook); nothing was written.
    Retries only transient HTTP 5xx / timeouts; an envelope ``success=false`` is
    deterministic and not retried.
    """
    detail = "no attempt"
    for attempt in range(retries + 1):
        try:
            resp = client.post(
                url,
                content=body,
                headers={"Content-Type": "application/json"},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            detail = type(exc).__name__
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            return "failed", detail
        if resp.status_code >= 500:
            detail = f"HTTP {resp.status_code}: {resp.text[:160]}"
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            return "failed", detail
        if resp.status_code >= 400:
            return "failed", f"HTTP {resp.status_code}: {resp.text[:160]}"
        envelope = _parse_envelope(resp)
        if envelope is None:
            return "skipped", resp.text[:160]
        if envelope.get("success") is True:
            outcomes = envelope.get("outcomes") or []
            summary = ", ".join(
                f"{o.get('op_type')}={o.get('record_id') or o.get('success')}"
                for o in outcomes
            )
            return "ok", summary
        return "failed", json.dumps(envelope.get("outcomes") or envelope)[:300]
    return "failed", detail


def _sent_key(row: dict[str, Any]) -> str:
    """Stable resume key for ``sent.log`` — must match the build's dedup key.

    The API build dedups on ``(source, source_id)`` (distinct mentions can share
    a URL), so resume keys on the same composite when ``source_id`` is present,
    falling back to the URL only when it isn't. Keying on URL alone would let a
    resumed run permanently skip a later same-URL mention.
    """
    source_id = (row.get("Source ID") or "").strip()
    if source_id:
        source = (row.get("Source") or "").strip().lower()
        return f"sid:{source}|{source_id}"
    return "url:" + (row.get("URL") or "").strip()


def _already_sent(
    row: dict[str, Any],
    already: set[str],
    *,
    allow_url_fallback: bool,
) -> bool:
    """True if ``row`` is recorded in ``sent.log`` under an accepted key.

    Always honors the current composite key. The legacy bare-URL fallback is
    enabled ONLY for the CSV flow (``allow_url_fallback``): CSV dedups by URL, so
    a bare-URL entry unambiguously identifies the row. The API flow must NOT use
    it — distinct API mentions can share a URL, so matching a stale bare-URL
    entry would silently skip a valid mention (data loss). Re-sending instead is
    idempotent (Attio upserts), so the API path errs toward re-send, not skip.
    """
    if _sent_key(row) in already:
        return True
    if not allow_url_fallback:
        return False
    url = (row.get("URL") or "").strip()
    return bool(url) and url in already  # legacy bare-URL entries (CSV only)


def send(
    jsonl_path: Path,
    *,
    source: str,
    endpoint_url: str,
    relevance: str,
    apply: bool,
    limit: int | None,
    timeout: float,
    sent_log: Path,
) -> None:
    """Phase 2: map → validate → POST each mention (dry-run unless ``apply``)."""
    already: set[str] = set()
    if sent_log.exists():
        already = {
            line.strip() for line in sent_log.read_text().splitlines() if line.strip()
        }

    rows = _load_rows(jsonl_path)
    # Legacy bare-URL resume entries are honored only for CSV (it dedups by URL);
    # the API flow accepts only the stable composite key — see _already_sent.
    allow_url_fallback = source == "csv"
    todo = [
        r
        for r in rows
        if not _already_sent(r, already, allow_url_fallback=allow_url_fallback)
    ]
    if limit is not None:
        todo = todo[:limit]

    mode = "APPLY (real POST)" if apply else "DRY-RUN (no POST)"
    print(f"\n[send] {mode}  endpoint={endpoint_url}")
    print(
        f"[send] relevance={relevance}  total={len(rows)} already_sent={len(already)} "
        f"to_process={len(todo)}",
    )

    ok = skipped = failed = 0
    client = httpx.Client(timeout=timeout)
    try:
        for i, row in enumerate(todo, start=1):
            url = (row.get("URL") or "").strip()
            payload = build_webhook_payload(
                row,
                relevance=relevance,
                source_file=row.get("_source_file", "unknown"),
            )
            # Identity fields must be non-empty. build_webhook_payload coerces
            # missing values to "" and Mention accepts empty strings, so the
            # model alone would let a blank source_id through — guard explicitly.
            data = payload["data"]
            missing = [
                field
                for field in ("url", "source", "source_id")
                if not str(data.get(field) or "").strip()
            ]
            if missing:
                print(
                    f"  [skip] missing required {missing} url={url[:70] or '(blank)'}",
                )
                skipped += 1
                continue
            try:
                webhook = Webhook.model_validate(payload)
            except Exception as exc:  # noqa: BLE001 — log + skip any invalid row
                print(f"  [skip] invalid payload url={url}: {str(exc)[:160]}")
                skipped += 1
                continue
            if not webhook.attio_is_valid_webhook():
                print(
                    f"  [skip] {webhook.attio_get_invalid_webhook_error_msg()} url={url}",
                )
                skipped += 1
                continue

            if not apply:
                if i <= 5:
                    print(
                        f"  [dry] would POST source={payload['data']['source']} "
                        f"relevance={payload['data']['relevance_score']} url={url}",
                    )
                ok += 1
                continue

            status, detail = _deliver(client, endpoint_url, orjson.dumps(payload))
            if status == "ok":
                ok += 1
                with sent_log.open("a") as fh:
                    fh.write(_sent_key(row) + "\n")
                if i <= 5:
                    print(f"  [ok] {url[:70]} :: {detail[:120]}")
            elif status == "skipped":
                skipped += 1
                print(f"  [skip] {detail[:140]} url={url[:70]}")
            else:
                failed += 1
                print(f"  [FAIL] {url[:70]} :: {detail[:200]}")
            if i % 20 == 0:
                print(
                    f"  [progress] {i}/{len(todo)} ok={ok} failed={failed} skipped={skipped}",
                )
    finally:
        client.close()

    verb = "sent" if apply else "would send"
    print(f"[send] done: {verb}={ok} skipped={skipped} failed={failed}")
    if not apply:
        print("[send] dry-run only — re-run with --apply to POST for real.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="directory of octolens-mentions-*.csv (or set OCTOLENS_DATA_DIR)",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--source",
        choices=("csv", "api"),
        default="csv",
        help=(
            "where mentions come from: 'csv' (local Octolens exports, default) or "
            "'api' (live v2 REST API, real relevance + all-time; needs OCTOLENS_API_KEY)"
        ),
    )
    parser.add_argument(
        "--include-low",
        action="store_true",
        help=(
            "api source only: also fetch low-relevance mentions (includeAll). "
            "The webhook drops 'low' before Attio, so this only adds skipped POSTs"
        ),
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="api source only: mentions per page (1-100)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="api source only: cap pages fetched (safety for exploratory runs)",
    )
    parser.add_argument(
        "--keyword",
        action="append",
        dest="keywords_filter",
        default=None,
        help=(
            "api source only: tracked keyword text to filter by, server-side "
            "(repeatable). Defaults to dlt + dlthub"
        ),
    )
    parser.add_argument(
        "--keyword-id",
        action="append",
        dest="keyword_ids",
        type=int,
        default=None,
        help=(
            "api source only: numeric keyword id to filter by (repeatable). "
            "Bypasses the keyword-text lookup; takes precedence over --keyword"
        ),
    )
    parser.add_argument(
        "--keyword-filtered",
        action="store_true",
        help=(
            "api source only: fast path — filter server-side by the dlt/dlthub "
            "tracked keywords instead of walking the full org feed. Misses "
            "content/URL-only matches the default exhaustive pull would catch. "
            "Implied when --keyword/--keyword-id is given"
        ),
    )
    parser.add_argument("--rebuild", action="store_true", help="regenerate the JSONL")
    parser.add_argument(
        "--relevance",
        choices=("low", "medium", "high", "unknown"),
        default="unknown",
        help="relevance_score stamped on every backfilled mention",
    )
    parser.add_argument("--send", action="store_true", help="run the send phase")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually POST (else dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap mentions processed",
    )
    parser.add_argument(
        "--endpoint-url",
        default=modal_url_for_app(Webhook.attio_get_app_name()),
        help="Attio webhook URL (defaults from MODAL_WORKSPACE; override for prod)",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    if args.source == "api":
        # Exhaustive (full-feed) is the default for recall parity with CSV; the
        # keyword filter is an explicit opt-in, also implied by naming a keyword.
        explicit_keyword = bool(args.keywords_filter) or bool(args.keyword_ids)
        all_mentions = not (args.keyword_filtered or explicit_keyword)
        jsonl_path = build_jsonl_from_api(
            args.out_dir,
            rebuild=args.rebuild,
            include_low=args.include_low,
            page_size=args.page_size,
            max_pages=args.max_pages,
            keyword_texts=args.keywords_filter or ["dlt", "dlthub"],
            keyword_ids=args.keyword_ids,
            all_mentions=all_mentions,
            # Reuse a prior build only in the send handoff; a plain build/preview
            # always re-fetches live so the API path is never silently stale.
            allow_reuse=args.send,
        )
    else:
        if args.data_dir is None:
            raise SystemExit(
                "No data dir: pass --data-dir or set OCTOLENS_DATA_DIR "
                "(e.g. ~/Documents/ai/data/octolens).",
            )
        jsonl_path = build_jsonl(args.data_dir, args.out_dir, rebuild=args.rebuild)

    if not args.send:
        # Preview a few mapped+validated payloads so the mapper is verifiable
        # without sending anything.
        print("\n[preview] sample mapped payloads:")
        for row in _load_rows(jsonl_path)[:3]:
            payload = build_webhook_payload(
                row,
                relevance=args.relevance,
                source_file=row.get("_source_file", "unknown"),
            )
            Webhook.model_validate(payload)  # raises if the mapper is wrong
            print("  " + orjson.dumps(payload["data"]).decode()[:200] + " ...")
        print(
            "\n[preview] build complete. Re-run with --send (then --apply) to deliver.",
        )
        return

    send(
        jsonl_path,
        source=args.source,
        endpoint_url=args.endpoint_url,
        relevance=args.relevance,
        apply=args.apply,
        limit=args.limit,
        timeout=args.timeout,
        sent_log=args.out_dir / "sent.log",
    )


if __name__ == "__main__":
    main()
