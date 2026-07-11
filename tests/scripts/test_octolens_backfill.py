"""Unit tests for ``scripts/octolens-mentions-backfill.py``.

The script filename is hyphenated, so it's loaded via ``importlib`` rather than
imported. Covers the cross-source cache guard (a CSV build can't be silently
reused under ``--source api`` or vice versa).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from libs.octolens.models import ApiMention

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "octolens-mentions-backfill.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("octolens_backfill", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_module()


_FP = "api|ids=[30412, 31833]|low=0|page=100|max=None"


def _seed(tmp_path: Path, marker: str | None) -> Path:
    jsonl = tmp_path / "mentions.jsonl"
    jsonl.write_text("{}\n")
    if marker is not None:
        (tmp_path / _MODULE._BUILD_MARKER).write_text(marker + "\n")
    return jsonl


def test_reuse_returns_none_without_existing_jsonl(tmp_path: Path) -> None:
    jsonl = tmp_path / "mentions.jsonl"
    assert _MODULE._reuse_cached_jsonl(jsonl, fingerprint=_FP, rebuild=False) is None


def test_reuse_returns_none_when_rebuild(tmp_path: Path) -> None:
    jsonl = _seed(tmp_path, _FP)
    assert _MODULE._reuse_cached_jsonl(jsonl, fingerprint=_FP, rebuild=True) is None


def test_reuse_ok_when_fingerprint_matches(tmp_path: Path) -> None:
    jsonl = _seed(tmp_path, _FP)
    assert _MODULE._reuse_cached_jsonl(jsonl, fingerprint=_FP, rebuild=False) == jsonl


def test_reuse_refused_on_source_mismatch(tmp_path: Path) -> None:
    jsonl = _seed(tmp_path, "csv")
    with pytest.raises(SystemExit, match="different config"):
        _MODULE._reuse_cached_jsonl(jsonl, fingerprint=_FP, rebuild=False)


def test_reuse_refused_on_filter_param_change(tmp_path: Path) -> None:
    # Same source, but a different keyword filter / relevance window must not
    # silently reuse the prior build's output.
    jsonl = _seed(tmp_path, "api|ids=[30412]|low=0|page=100|max=None")
    with pytest.raises(SystemExit, match="different config"):
        _MODULE._reuse_cached_jsonl(jsonl, fingerprint=_FP, rebuild=False)


def test_sent_key_uses_source_id_distinguishing_same_url() -> None:
    # Two distinct mentions sharing a URL get distinct resume keys (source_id),
    # so a resumed --send can't skip the second.
    a = {"URL": "https://x/y", "Source": "reddit", "Source ID": "c1"}
    b = {"URL": "https://x/y", "Source": "reddit", "Source ID": "c2"}
    assert _MODULE._sent_key(a) != _MODULE._sent_key(b)
    assert _MODULE._sent_key(a) == "sid:reddit|c1"


def test_sent_key_falls_back_to_url_without_source_id() -> None:
    row = {"URL": "https://x/y", "Source": "reddit", "Source ID": ""}
    assert _MODULE._sent_key(row) == "url:https://x/y"


def test_already_sent_csv_honors_legacy_bare_url() -> None:
    row = {"URL": "https://x/y", "Source": "reddit", "Source ID": "c1"}
    # CSV flow: a legacy bare-URL sent.log entry still counts as sent.
    assert _MODULE._already_sent(row, {"https://x/y"}, allow_url_fallback=True) is True
    # The new composite key always counts.
    assert (
        _MODULE._already_sent(row, {"sid:reddit|c1"}, allow_url_fallback=True) is True
    )


def test_already_sent_api_ignores_bare_url_to_avoid_skipping() -> None:
    # API flow: distinct mentions can share a URL, so a stale bare-URL entry must
    # NOT mark this row as sent — that would silently skip a valid mention.
    row = {"URL": "https://x/y", "Source": "reddit", "Source ID": "c2"}
    assert (
        _MODULE._already_sent(row, {"https://x/y"}, allow_url_fallback=False) is False
    )
    # Its own composite key still counts.
    assert (
        _MODULE._already_sent(row, {"sid:reddit|c2"}, allow_url_fallback=False) is True
    )


def test_reuse_rebuilds_when_marker_missing(tmp_path: Path) -> None:
    # A markerless JSONL (legacy dir or a build interrupted before the marker
    # was stamped) has unknown provenance, so reuse returns None → rebuild,
    # rather than reusing it or hard-aborting. Holds for both source fingerprints.
    jsonl = _seed(tmp_path, None)
    assert _MODULE._reuse_cached_jsonl(jsonl, fingerprint="csv", rebuild=False) is None
    assert _MODULE._reuse_cached_jsonl(jsonl, fingerprint=_FP, rebuild=False) is None


def test_filter_and_write_atomic_and_stamps_fingerprint(tmp_path: Path) -> None:
    jsonl = tmp_path / "mentions.jsonl"
    # A stale marker from a prior build must be replaced, not left behind.
    (tmp_path / _MODULE._BUILD_MARKER).write_text("api|stale\n")
    rows = {
        "u": {
            "URL": "https://github.com/dlt-hub/dlt/issues/1",
            "Source": "github",
            "Source ID": "1",
            "Body": "dlthub is great",
            "Keyword": "dlthub",
            "_relevance_score": "high",
        },
    }
    seen = _MODULE._filter_and_write(rows, jsonl, fingerprint="api|fresh")
    assert len(seen) == 1
    assert jsonl.exists()
    assert not (tmp_path / "mentions.jsonl.tmp").exists()  # temp cleaned up
    assert (tmp_path / _MODULE._BUILD_MARKER).read_text().strip() == "api|fresh"


# --- allow_reuse: api builds re-fetch unless in the send handoff ----------


class _FakeClient:
    """Minimal stand-in for OctolensClient used to assert fetch-vs-reuse."""

    def __init__(self, mentions: object = ()) -> None:
        self.fetched = False
        self.filters: object = "unset"
        self._mentions = mentions

    def list_keywords(self) -> list[dict[str, object]]:
        return [{"id": 30412, "keyword": "dlthub"}, {"id": 31833, "keyword": "dlt"}]

    def list_mentions(self, **kwargs: object):  # noqa: ANN003 - test stub
        self.fetched = True
        self.filters = kwargs.get("filters")
        return iter(self._mentions)  # type: ignore[arg-type]

    def close(self) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        pass


_CSV_FP = "api|texts=['dlt', 'dlthub']|low=0|page=100|max=None"


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    def _from_env(**_kwargs: object) -> _FakeClient:
        return fake

    monkeypatch.setattr(_MODULE.OctolensClient, "from_env", _from_env)


def _build_api(
    tmp_path: Path,
    *,
    allow_reuse: bool,
    all_mentions: bool = False,
) -> Path:
    return _MODULE.build_jsonl_from_api(
        tmp_path,
        rebuild=False,
        include_low=False,
        page_size=100,
        max_pages=None,
        keyword_texts=["dlt", "dlthub"],
        keyword_ids=None,
        all_mentions=all_mentions,
        allow_reuse=allow_reuse,
    )


def test_api_build_refetches_when_reuse_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A matching cache exists, but a plain build (allow_reuse=False) must still
    # hit the live API rather than serve the stale JSONL.
    _seed(tmp_path, _CSV_FP)
    fake = _FakeClient()
    _patch_client(monkeypatch, fake)
    _build_api(tmp_path, allow_reuse=False)
    assert fake.fetched is True


def test_api_build_reuses_cache_in_send_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path, _CSV_FP)
    fake = _FakeClient()
    _patch_client(monkeypatch, fake)
    result = _build_api(tmp_path, allow_reuse=True)
    assert result == tmp_path / "mentions.jsonl"
    assert fake.fetched is False  # reused; never fetched


def test_cached_reuse_needs_no_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Replaying a cached build (send handoff) hits only the public webhook, so it
    # must not require OCTOLENS_API_KEY — from_env() is never reached on reuse.
    monkeypatch.delenv("OCTOLENS_API_KEY", raising=False)
    _seed(tmp_path, _CSV_FP)
    result = _build_api(tmp_path, allow_reuse=True)
    assert result == tmp_path / "mentions.jsonl"


def test_keyword_filtered_mode_sets_server_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient()
    _patch_client(monkeypatch, fake)
    _build_api(tmp_path, allow_reuse=False, all_mentions=False)
    assert isinstance(fake.filters, dict)
    assert sorted(fake.filters["keyword"]) == [30412, 31833]


def test_exhaustive_mode_sends_no_keyword_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default (exhaustive): include_mention narrows client-side, no server filter.
    fake = _FakeClient()
    _patch_client(monkeypatch, fake)
    _build_api(tmp_path, allow_reuse=False, all_mentions=True)
    assert fake.filters is None


def test_api_dedups_by_source_id_not_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two distinct mentions sharing one URL must both survive — dedup keys on
    # source_id, not URL.
    shared = "https://www.reddit.com/r/dataengineering/comments/x/dlthub_rocks/"
    base = {
        "url": shared,
        "source": "reddit",
        "timestamp": "2026-05-10 11:55:53.000",
        "keywords": [{"id": 30412, "keyword": "dlthub"}],
    }
    m1 = ApiMention.model_validate({**base, "sourceId": "c1", "body": "dlthub a"})
    m2 = ApiMention.model_validate({**base, "sourceId": "c2", "body": "dlthub b"})
    fake = _FakeClient([m1, m2])
    _patch_client(monkeypatch, fake)
    _MODULE.build_jsonl_from_api(
        tmp_path,
        rebuild=True,
        include_low=False,
        page_size=100,
        max_pages=None,
        keyword_texts=["dlt", "dlthub"],
        keyword_ids=None,
        all_mentions=True,
        allow_reuse=False,
    )
    lines = (tmp_path / "mentions.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2  # both kept; URL-dedup would have collapsed to 1
