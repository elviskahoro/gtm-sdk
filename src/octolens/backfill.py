"""Pure helpers for the historical Octolens CSV → Attio backfill.

These functions are I/O-free so they can be unit-tested without touching the
filesystem or the network. The operator script
``scripts/octolens-backfill-mentions.py`` wires them to CSV reading and the
Modal POST loop.

Scope rationale (see the backfill plan): the raw ``dlt`` keyword Octolens
assigned is ~99% noise (crypto "DLT", incidental string hits), while ``dlthub``
is unambiguous. So a row is in scope iff ``dlthub`` appears in the keyword set
*or anywhere in the post text*, OR ``dlt`` is a keyword *and* the text carries a
dlthub-library content signal (``DLT_SIGNALS``). ``include_mention`` returns the
reason so the build step can print an auditable table.
"""

from __future__ import annotations

from typing import Any

# Phrases that indicate a `dlt`-keyword mention is really about the dlthub
# Python library (not "distributed ledger technology" or an incidental match).
# Editable: the build dry-run prints each inclusion reason so this list can be
# tuned, then re-run with --rebuild.
DLT_SIGNALS: tuple[str, ...] = (
    "dlthub",
    "dlt-hub",
    "data load tool",
    "pip install dlt",
    "import dlt",
    "dlt pipeline",
    "@dlthub",
    "dlt destination",
    "dlt source",
)

# dlthub-owned URL markers. The brand in a URL host/path is a strong, explicit
# signal (the dltHub account, the dlt-hub GitHub org, the docs/run sites), so we
# match it as its own rule instead of substring-scanning the whole URL as free
# text — that keeps an incidental "dlt"/"dlthub" in some unrelated site's path
# or query string from being admitted as a false positive. Lowercased substrings.
_DLTHUB_URL_MARKERS: tuple[str, ...] = (
    "dlthub.com",
    "github.com/dlt-hub",
    "/dlt-hub/",
    "/dlthub",
    "/r/dlthub",
    "dlt.run",
)

# CSV column → not all map 1:1 onto Mention fields, so the mapper is explicit.
# Required-by-model string fields that the CSV may leave blank are coerced to ""
# rather than dropped; identity/timestamp gaps are left for model validation to
# reject (the script logs + skips those).
_REQUIRED_STR_COERCE = ("body", "author")


def split_csv_list(value: str | None) -> list[str]:
    """Split a comma-separated cell (Keyword/Tags) into trimmed, non-empty parts.

    Octolens exports multi-valued keywords/tags as a single comma-joined cell
    (e.g. ``"databricks, dlt"``). Case is preserved; callers lowercase when
    matching.
    """
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def coerce_view_id(value: Any) -> int | None:
    """Return an int view id, or None when the cell is blank/non-numeric.

    The CSV ``View ID`` column mixes integers with sentinels like ``"all"``;
    the Mention model types ``view_id`` as ``int | None``.
    """
    if value is None:
        return None
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def normalize_source(value: str | None) -> str:
    """Lowercase + strip a source platform value (e.g. ``"YouTube"`` → ``"youtube"``)."""
    return (value or "").strip().lower()


def _content(row: dict[str, Any]) -> str:
    """Lowercased title + body. The URL is matched separately by _is_dlthub_url."""
    return f"{row.get('Title') or ''} {row.get('Body') or ''}".lower()


def _is_dlthub_url(url: str | None) -> bool:
    """True when the URL is a dlthub-owned property (explicit markers, not free text)."""
    lowered = (url or "").lower()
    return any(marker in lowered for marker in _DLTHUB_URL_MARKERS)


def include_mention(row: dict[str, Any]) -> tuple[bool, str | None]:
    """Decide whether a CSV row is an in-scope dlt/dlthub mention.

    Kept when:
    - ``dlthub`` is a keyword, appears in the title/body, or the URL is a
      dlthub-owned property → reason ``"dlthub-anywhere"``;
    - ``dlt`` is a keyword AND the title/body carries a dlthub-library content
      signal (or the URL is dlthub-owned) → reason ``"dlt+signal"``.
    The URL is matched only against explicit dlthub markers, never scanned as
    free text, so an incidental substring can't admit a false positive. Returns
    ``(False, None)`` otherwise.
    """
    keyword_tokens = {token.lower() for token in split_csv_list(row.get("Keyword"))}
    content = _content(row)
    dlthub_url = _is_dlthub_url(row.get("URL"))

    if "dlthub" in keyword_tokens or "dlthub" in content or dlthub_url:
        return True, "dlthub-anywhere"
    if "dlt" in keyword_tokens and (
        any(signal in content for signal in DLT_SIGNALS) or dlthub_url
    ):
        return True, "dlt+signal"
    return False, None


def _primary_keyword(keywords: list[str]) -> str:
    """Pick a meaningful primary keyword: prefer dlthub/dlt, else the first token."""
    lowered = [keyword.lower() for keyword in keywords]
    for preferred in ("dlthub", "dlt"):
        if preferred in lowered:
            return preferred
    return keywords[0] if keywords else ""


def _opt(value: Any) -> str | None:
    """Empty/whitespace cells → None for optional string fields."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_webhook_payload(
    row: dict[str, Any],
    *,
    relevance: str,
    source_file: str,
) -> dict[str, Any]:
    """Map one CSV row to the Octolens webhook payload ``{"action", "data"}``.

    Keys are the Mention field names (snake_case); the model has
    ``populate_by_name=True`` so they bind without aliases. ``relevance`` is
    stamped verbatim (the backfill uses ``"unknown"`` — the CSV carries no
    score). Required string fields absent from the CSV are coerced to ""; the
    caller validates the result against the Webhook model and skips rows that
    fail (missing url/source/source_id or an unparseable timestamp).
    """
    keywords = split_csv_list(row.get("Keyword"))
    data: dict[str, Any] = {
        "url": (row.get("URL") or "").strip(),
        "title": _opt(row.get("Title")),
        "body": row.get("Body") or "",
        "timestamp": (row.get("Timestamp") or "").strip(),
        "image_url": _opt(row.get("Image URL")),
        "source": normalize_source(row.get("Source")),
        "source_id": (row.get("Source ID") or "").strip(),
        "author": (row.get("Author") or "").strip(),
        "author_avatar_url": _opt(row.get("Author Avatar URL")),
        "author_profile_link": _opt(row.get("Author Profile Link")),
        "relevance_score": relevance,
        "relevance_comment": (
            f"Backfilled from Octolens CSV export ({source_file}); "
            "relevance not scored at export time."
        ),
        "language": _opt(row.get("Language")),
        "keyword": _primary_keyword(keywords),
        "keywords": keywords,
        "sentiment_label": _opt(row.get("Sentiment")),
        "tags": split_csv_list(row.get("Tags")),
        "view_id": coerce_view_id(row.get("View ID")),
        "view_name": _opt(row.get("View Name")),
        "view_keywords": [],
        "subreddit": None,
        "bookmarked": False,
    }
    return {"action": "mention_created", "data": data}
