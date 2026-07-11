"""Pure helpers for the historical Octolens CSV → Attio backfill.

These functions are I/O-free so they can be unit-tested without touching the
filesystem or the network. The operator script
``scripts/octolens-mentions-backfill.py`` wires them to CSV reading and the
Modal POST loop.

Scope rationale (see the backfill plan): the raw ``dlt`` keyword Octolens
assigned is ~99% noise (crypto "DLT", incidental string hits), while ``dlthub``
is unambiguous. So a row is in scope iff ``dlthub`` appears in the keyword set,
the title/body, or a dlthub-owned URL; OR ``dlt`` is a keyword *and* the
title/body carries a dlthub-library content signal (``DLT_SIGNALS``).
``include_mention`` returns the reason so the build step can print an auditable
table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from libs.octolens.models import ApiMention

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

# dlthub-owned web properties, matched by *parsed* hostname (and a path prefix
# where the host is shared, e.g. github.com). Parsing host/path — rather than
# substring-scanning the raw URL — keeps an incidental "dlthub"/"dlt-hub" in some
# unrelated site's path or query string from being admitted as a false positive.
_DLTHUB_HOSTS: tuple[str, ...] = ("dlthub.com", "dlt.run")  # whole host is dlthub
_DLTHUB_HOST_PATHS: tuple[tuple[str, str], ...] = (
    ("github.com", "/dlt-hub"),  # the dlt-hub GitHub org
    ("twitter.com", "/dlthub"),
    ("x.com", "/dlthub"),
    ("reddit.com", "/r/dlthub"),
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
    """Lowercased title + body. The URL is matched separately by is_dlthub_url."""
    return f"{row.get('Title') or ''} {row.get('Body') or ''}".lower()


def is_dlthub_url(url: str | None) -> bool:
    """True when the URL is a dlthub-owned property.

    Matches the *parsed* hostname (and, for shared hosts like github.com, a path
    prefix) against an allowlist — never a raw-substring scan — so an incidental
    "dlthub"/"dlt-hub" elsewhere in the URL cannot admit a false positive.
    """
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = (parts.path or "").lower().rstrip("/")
    if any(host == h or host.endswith(f".{h}") for h in _DLTHUB_HOSTS):
        return True
    return any(
        (host == h or host.endswith(f".{h}"))
        and (path == seg or path.startswith(f"{seg}/"))
        for h, seg in _DLTHUB_HOST_PATHS
    )


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
    dlthub_url = is_dlthub_url(row.get("URL"))

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


# Octolens v2 API relevanceScore (0=high, 1=medium, 2=low) → our RelevanceScore.
# The CSV path has no score and stamps "unknown"; the API path carries a real
# verdict, so we preserve it here. Low survives the mapping but the live webhook's
# DEFAULT_FILTERS drops it before Attio (matching live behaviour).
RELEVANCE_BY_SCORE: dict[int, str] = {0: "high", 1: "medium", 2: "low"}

# API source platforms that the inbound-webhook Mention model accepts under a
# different name. Everything else outside libs.octolens.Source is skipped by the
# api build with a logged count (the webhook itself couldn't ingest it).
_API_SOURCE_ALIASES: dict[str, str] = {"reddit_comment": "reddit"}


def relevance_from_api(relevance_score: Any, relevance: Any) -> str:
    """Map the v2 API's ``relevanceScore``/``relevance`` to a RelevanceScore.

    Prefer the numeric ``relevanceScore`` (0=high / 1=medium / 2=low). When it's
    absent, fall back to the coarse ``relevance`` verdict: ``not_relevant`` → low
    (dropped by the webhook), anything else → medium.
    """
    if relevance_score is not None:
        try:
            return RELEVANCE_BY_SCORE.get(int(relevance_score), "medium")
        except (TypeError, ValueError):
            pass
    if isinstance(relevance, str) and relevance.strip().lower() == "not_relevant":
        return "low"
    return "medium"


def api_mention_to_row(mention: ApiMention) -> dict[str, Any]:
    """Map one v2-API :class:`ApiMention` to the CSV-shaped row dict.

    Producing the same Title-Case row shape the CSV reader yields lets
    :func:`include_mention` and :func:`build_webhook_payload` work unchanged
    across both sources. Unlike the CSV path, the API carries a real relevance
    verdict, stamped under the internal ``_relevance_score`` /
    ``_relevance_comment`` keys (underscore-prefixed like ``_source_file``, so
    they never collide with a CSV column); ``build_webhook_payload`` prefers
    those over its ``relevance`` argument.
    """
    keywords = [kw.keyword for kw in mention.keywords if kw.keyword]
    raw_source = (mention.source or "").strip().lower()
    source = _API_SOURCE_ALIASES.get(raw_source, raw_source)
    relevance = relevance_from_api(mention.relevance_score, mention.relevance)
    comment = _opt(mention.relevance_comment) or (
        f"Backfilled from Octolens v2 API (relevance={relevance})."
    )
    return {
        "URL": (mention.url or "").strip(),
        "Title": mention.title,
        "Body": mention.body or "",
        "Timestamp": (mention.timestamp or "").strip(),
        "Image URL": mention.image_url,
        "Source": source,
        "Source ID": (mention.source_id or "").strip(),
        # Fall back to authorName when the handle field is empty — some sources
        # populate one but not the other; an empty author loses person linkage.
        "Author": mention.author or mention.author_name or "",
        "Author Avatar URL": mention.author_avatar,
        "Author Profile Link": mention.author_url,
        "Language": mention.language,
        "Keyword": ", ".join(keywords),
        "Sentiment": mention.sentiment,
        "Tags": ", ".join(str(tag) for tag in mention.tags),
        "View ID": None,
        "View Name": None,
        "_source_file": "octolens-api-v2",
        "_relevance_score": relevance,
        "_relevance_comment": comment,
    }


def build_webhook_payload(
    row: dict[str, Any],
    *,
    relevance: str,
    source_file: str,
) -> dict[str, Any]:
    """Map one row (CSV- or API-shaped) to the webhook payload ``{"action", "data"}``.

    Keys are the Mention field names (snake_case); the model has
    ``populate_by_name=True`` so they bind without aliases. Required string
    fields absent from the source are coerced to ""; the caller validates the
    result against the Webhook model and skips rows that fail (missing
    url/source/source_id or an unparseable timestamp).

    Relevance: a row may carry a real verdict under ``_relevance_score`` /
    ``_relevance_comment`` (set by :func:`api_mention_to_row`); when present those
    win. Otherwise the ``relevance`` argument is stamped verbatim — the CSV
    backfill passes ``"unknown"`` because those exports carry no score.
    """
    keywords = split_csv_list(row.get("Keyword"))
    relevance_score = _opt(row.get("_relevance_score")) or relevance
    relevance_comment = _opt(row.get("_relevance_comment")) or (
        f"Backfilled from Octolens CSV export ({source_file}); "
        "relevance not scored at export time."
    )
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
        "relevance_score": relevance_score,
        "relevance_comment": relevance_comment,
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
