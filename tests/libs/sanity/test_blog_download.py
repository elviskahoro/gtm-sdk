"""Tests for blog download path-safety and fetch error handling."""

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from libs.sanity.client import SanityConfig
from libs.sanity.errors import SanityQueryError, UnsafeArchiveDirError
from src.sanity.blog_download import (
    _SNAPSHOT_MARKER,  # pyright: ignore[reportPrivateUsage]
    _SUPPORTS_DIR_FD,  # pyright: ignore[reportPrivateUsage]
    _write_no_follow,  # pyright: ignore[reportPrivateUsage]
    download_blog_posts,
    is_safe_slug,
)


def _make_snapshot(path: Path) -> Path:
    """Create a directory that looks like one this tool wrote (carries marker)."""
    path.mkdir(parents=True)
    (path / "post.json").write_text("{}", encoding="utf-8")
    (path / _SNAPSHOT_MARKER).write_text("x", encoding="utf-8")
    return path


def test_safe_slug_accepts_normal_slugs():
    assert is_safe_slug("duckdb-1M-downloads-users")


@pytest.mark.parametrize(
    "bad",
    ["", ".", "..", "a/b", "..\\evil", "../escape", "with\x00null"],
)
def test_safe_slug_rejects_traversal(bad: str):
    assert not is_safe_slug(bad)


def _raw(slug: str) -> dict[str, Any]:
    return {
        "metadata": {"slug": {"current": slug}, "title": slug},
        "slug": slug,
        "title": slug,
        "body": [],
    }


def test_download_skips_unsafe_slugs(tmp_path: Path):
    raw_posts = [_raw("good-post"), _raw("../escape")]
    with patch("src.sanity.blog_download.fetch_blog_posts_raw", return_value=raw_posts):
        result = download_blog_posts(tmp_path, config=SanityConfig())

    assert result.written == 1
    assert result.skipped == 1
    assert result.slugs == ["good-post"]
    # The traversal slug must not have written anything outside out/blogs/.
    assert (tmp_path / "blogs" / "good-post" / "index.md").exists()
    assert not (tmp_path / "escape").exists()


def test_frontmatter_is_valid_yaml_with_special_chars(tmp_path: Path):
    import yaml

    raw = _raw("tricky")
    raw["title"] = 'Line one: with "quotes"\nLine two'
    raw["metadata"]["title"] = raw["title"]
    raw["description"] = "ends with colon:"
    with patch("src.sanity.blog_download.fetch_blog_posts_raw", return_value=[raw]):
        download_blog_posts(tmp_path, config=SanityConfig())

    text = (tmp_path / "blogs" / "tricky" / "index.md").read_text()
    fm_block = text.split("---\n", 2)[1]
    parsed = yaml.safe_load(fm_block)
    assert parsed["title"] == 'Line one: with "quotes"\nLine two'
    assert parsed["description"] == "ends with colon:"
    assert parsed["source"] == "https://dlthub.com/blog/tricky"


def test_prune_removes_stale_snapshots(tmp_path: Path):
    blogs = tmp_path / "blogs"
    # A stale snapshot we own (carries the marker) and an unrelated dir to keep.
    stale = _make_snapshot(blogs / "old-post")
    keep = blogs / "user-notes"
    keep.mkdir(parents=True)
    (keep / "scratch.txt").write_text("mine", encoding="utf-8")

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig(), prune=True)

    assert result.pruned == 1
    assert (blogs / "fresh-post" / "index.md").exists()
    assert not stale.exists()
    # A directory without our marker is not ours — leave it alone.
    assert (keep / "scratch.txt").exists()


def test_prune_ignores_unowned_dir_with_post_json(tmp_path: Path):
    # A user dir that happens to contain a post.json but lacks our marker must
    # never be deleted, even when its name is absent from the live corpus.
    blogs = tmp_path / "blogs"
    foreign = blogs / "not-ours"
    foreign.mkdir(parents=True)
    (foreign / "post.json").write_text("{}", encoding="utf-8")

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig(), prune=True)

    assert result.pruned == 0
    assert foreign.exists()


def test_prune_keeps_snapshot_for_present_but_skipped_post(tmp_path: Path):
    # A post still in the corpus but skipped this run (unsafe slug) must not have
    # its prior snapshot — stored under its real slug — pruned.
    blogs = tmp_path / "blogs"
    prior = _make_snapshot(blogs / "exists-but-skipped")

    skipped = _raw("exists-but-skipped")
    skipped["slug"] = "exists-but-skipped"

    def only_fresh_is_safe(slug: str) -> bool:
        return slug == "fresh-post"

    # Force the skip path while keeping the real slug present in the corpus.
    with patch(
        "src.sanity.blog_download.is_safe_slug",
        side_effect=only_fresh_is_safe,
    ):
        with patch(
            "src.sanity.blog_download.fetch_blog_posts_raw",
            return_value=[_raw("fresh-post"), skipped],
        ):
            result = download_blog_posts(tmp_path, config=SanityConfig(), prune=True)

    assert result.skipped == 1
    assert result.pruned == 0
    assert prior.exists()


def test_prune_keeps_snapshot_when_only_casing_differs(tmp_path: Path):
    # A snapshot dir written as "My-Post" must survive a run whose live slug is
    # "my-post": on a case-insensitive volume they are the same directory, so a
    # case-sensitive prune would delete live content.
    blogs = tmp_path / "blogs"
    existing = _make_snapshot(blogs / "My-Post")

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("my-post")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig(), prune=True)

    assert result.pruned == 0
    assert existing.exists()


def test_no_prune_keeps_stale_snapshots(tmp_path: Path):
    stale = _make_snapshot(tmp_path / "blogs" / "old-post")

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig(), prune=False)

    assert result.pruned == 0
    assert stale.exists()


def test_post_json_preserves_raw_payload(tmp_path: Path):
    # A field outside the BlogPost model must still land in post.json.
    raw = _raw("keep-me")
    raw["_extraField"] = {"nested": [1, 2, 3]}
    with patch("src.sanity.blog_download.fetch_blog_posts_raw", return_value=[raw]):
        download_blog_posts(tmp_path, config=SanityConfig())

    import json

    written = json.loads((tmp_path / "blogs" / "keep-me" / "post.json").read_text())
    assert written["_extraField"] == {"nested": [1, 2, 3]}


def test_download_fails_fast_on_duplicate_slug(tmp_path: Path):
    from libs.sanity.errors import DuplicateSlugError

    first = _raw("dupe")
    first["_id"] = "post-A"
    second = _raw("dupe")
    second["_id"] = "post-B"

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[first, second],
    ):
        with pytest.raises(DuplicateSlugError, match="post-A.*post-B|dupe"):
            download_blog_posts(tmp_path, config=SanityConfig())


def test_download_fails_fast_on_case_aliased_slug(tmp_path: Path):
    # Distinct slugs in Sanity that collide on a case-insensitive filesystem.
    from libs.sanity.errors import DuplicateSlugError

    first = _raw("Foo")
    second = _raw("foo")

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[first, second],
    ):
        with pytest.raises(DuplicateSlugError):
            download_blog_posts(tmp_path, config=SanityConfig())


def test_duplicate_slug_aborts_before_any_write(tmp_path: Path):
    # A collision must be detected in the planning pass, so nothing is written —
    # the run never leaves a partially updated archive on disk.
    from libs.sanity.errors import DuplicateSlugError

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("dupe"), _raw("dupe")],
    ):
        with pytest.raises(DuplicateSlugError):
            download_blog_posts(tmp_path, config=SanityConfig())

    assert not (tmp_path / "blogs").exists()


def test_source_url_is_percent_encoded(tmp_path: Path):
    import yaml

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("a b&c")],
    ):
        download_blog_posts(tmp_path, config=SanityConfig())

    text = (tmp_path / "blogs" / "a b&c" / "index.md").read_text()
    fm_block = text.split("---\n", 2)[1]
    parsed = yaml.safe_load(fm_block)
    assert parsed["source"] == "https://dlthub.com/blog/a%20b%26c"


def test_fetch_raises_on_non_list_result():
    from libs.sanity.blog import fetch_blog_posts

    with patch("libs.sanity.blog.query", return_value={"unexpected": "shape"}):
        with pytest.raises(SanityQueryError):
            fetch_blog_posts(SanityConfig())


def test_fetch_raises_on_non_dict_row():
    from libs.sanity.blog import fetch_blog_posts

    with patch("libs.sanity.blog.query", return_value=[{"slug": "ok"}, "broken"]):
        with pytest.raises(SanityQueryError):
            fetch_blog_posts(SanityConfig())


@pytest.mark.parametrize(
    "bad",
    [
        "a:b",
        "a|b",
        "a?b",
        'a"b',
        "a*b",
        "trailing.",
        "trailing ",
        "CON",
        "nul.txt",
        "lpt3",
    ],
)
def test_safe_slug_rejects_platform_invalid_names(bad: str):
    assert not is_safe_slug(bad)


def test_prune_disabled_when_a_live_post_is_skipped(tmp_path: Path):
    # A still-live post with an unsafe slug this run must not let pruning delete
    # an unrelated stale snapshot — its real prior slug is unknown, so we can't
    # prove that snapshot is dead.
    blogs = tmp_path / "blogs"
    stale = _make_snapshot(blogs / "old-post")

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post"), _raw("../escape")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig(), prune=True)

    assert result.skipped == 1
    assert result.pruned == 0
    assert stale.exists()


def test_title_trailing_hash_is_escaped(tmp_path: Path):
    raw = _raw("x")
    raw["title"] = "Release #"
    raw["metadata"]["title"] = "Release #"
    with patch("src.sanity.blog_download.fetch_blog_posts_raw", return_value=[raw]):
        download_blog_posts(tmp_path, config=SanityConfig())

    text = (tmp_path / "blogs" / "x" / "index.md").read_text()
    assert "# Release \\#" in text


def test_download_fails_fast_on_unicode_aliased_slug(tmp_path: Path):
    import unicodedata

    from libs.sanity.errors import DuplicateSlugError

    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert nfc != nfd

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw(nfc), _raw(nfd)],
    ):
        with pytest.raises(DuplicateSlugError):
            download_blog_posts(tmp_path, config=SanityConfig())


@pytest.mark.parametrize("bad", ["con .txt", "AUX .md", "nul. ", "com1 .log"])
def test_safe_slug_rejects_reserved_names_with_trailing_padding(bad: str):
    assert not is_safe_slug(bad)


def test_download_refuses_to_clobber_unowned_dir(tmp_path: Path):
    # A pre-existing user dir at the target slug (no marker) must not be written
    # into or overwritten; the post is skipped instead.
    blogs = tmp_path / "blogs"
    foreign = blogs / "fresh-post"
    foreign.mkdir(parents=True)
    (foreign / "user.txt").write_text("mine", encoding="utf-8")

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig())

    assert result.written == 0
    assert result.skipped == 1
    assert (foreign / "user.txt").read_text() == "mine"
    assert not (foreign / "post.json").exists()


def test_download_overwrites_its_own_prior_snapshot(tmp_path: Path):
    # A re-run over a dir this tool created (carries the marker) is fine.
    blogs = tmp_path / "blogs"
    _make_snapshot(blogs / "fresh-post")

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig())

    assert result.written == 1
    assert "fresh-post" in (blogs / "fresh-post" / "index.md").read_text()


def test_download_refuses_symlink_target(tmp_path: Path):
    # A symlink (even broken) at the target slug must never be written through.
    blogs = tmp_path / "blogs"
    blogs.mkdir(parents=True)
    link = blogs / "fresh-post"
    link.symlink_to(tmp_path / "elsewhere")

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig())

    assert result.written == 0
    assert result.skipped == 1
    assert link.is_symlink()


def test_prune_ignores_symlinked_dir(tmp_path: Path):
    # A symlinked blogs/<slug> dir is not ours even if it points at a marked
    # snapshot; pruning must not delete or follow it.
    blogs = tmp_path / "blogs"
    blogs.mkdir(parents=True)
    real = _make_snapshot(tmp_path / "real-snap")
    link = blogs / "old-post"
    link.symlink_to(real)

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig(), prune=True)

    assert result.pruned == 0
    assert link.is_symlink()
    assert real.exists()


def test_download_resolves_symlinked_root_to_real_target(tmp_path: Path):
    # A symlinked --out-dir is resolved to its real target so writes/prunes
    # operate on one concrete tree (rather than diverging through the link).
    real = tmp_path / "real-out"
    real.mkdir()
    out_dir = tmp_path / "out"
    out_dir.symlink_to(real)

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post")],
    ):
        result = download_blog_posts(out_dir, config=SanityConfig())

    assert result.written == 1
    assert (real / "blogs" / "fresh-post" / "index.md").exists()


def test_download_refuses_symlinked_blogs_parent(tmp_path: Path):
    # A symlinked blogs/ parent would redirect every snapshot write into the
    # link target; it must be rejected before any write rather than followed.
    real = tmp_path / "real-blogs"
    real.mkdir()
    (tmp_path / "blogs").symlink_to(real)

    with (
        patch(
            "src.sanity.blog_download.fetch_blog_posts_raw",
            return_value=[_raw("fresh-post")],
        ),
        pytest.raises(UnsafeArchiveDirError),
    ):
        download_blog_posts(tmp_path, config=SanityConfig())

    assert not (real / "fresh-post").exists()


def test_download_refuses_file_at_blogs_parent(tmp_path: Path):
    # A plain file at blogs/ would crash the first mkdir with NotADirectoryError;
    # fail fast with a clear error instead.
    (tmp_path / "blogs").write_text("not a dir", encoding="utf-8")

    with (
        patch(
            "src.sanity.blog_download.fetch_blog_posts_raw",
            return_value=[_raw("fresh-post")],
        ),
        pytest.raises(UnsafeArchiveDirError),
    ):
        download_blog_posts(tmp_path, config=SanityConfig())


def test_download_removes_partial_snapshot_on_write_failure(tmp_path: Path):
    # A write that fails partway must not leave a half-written directory behind;
    # it is removed so a later run can recreate it cleanly.
    blogs = tmp_path / "blogs"

    def flaky(path: Path, data: str, *, dir_fd: int | None = None) -> None:
        if Path(path).name == "index.md":
            raise OSError("disk full")
        _write_no_follow(path, data, dir_fd=dir_fd)

    with (
        patch(
            "src.sanity.blog_download.fetch_blog_posts_raw",
            return_value=[_raw("fresh-post")],
        ),
        patch("src.sanity.blog_download._write_no_follow", side_effect=flaky),
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig())

    assert result.written == 0
    assert result.skipped == 1
    assert not (blogs / "fresh-post").exists()


def test_download_repairs_marker_only_partial_snapshot(tmp_path: Path):
    # A run killed mid-write leaves the marker (written first) but no leaves.
    # Because the marker proves ownership, a rerun overwrites it rather than
    # treating it as an unowned, un-repairable orphan.
    blogs = tmp_path / "blogs"
    partial = blogs / "fresh-post"
    partial.mkdir(parents=True)
    (partial / _SNAPSHOT_MARKER).write_text("x", encoding="utf-8")

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig())

    assert result.written == 1
    assert (partial / "post.json").exists()
    assert (partial / "index.md").exists()


def test_write_no_follow_handles_short_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # os.write may persist fewer bytes than requested; the writer must loop until
    # the whole payload lands rather than silently truncating a large file.
    target = tmp_path / "out.txt"
    payload = "x" * 5000
    real_write = os.write

    def short_write(fd: int, data: Any) -> int:
        # Persist at most one byte per call to force the write loop.
        return real_write(fd, bytes(data)[:1])

    monkeypatch.setattr("src.sanity.blog_download.os.write", short_write)
    _write_no_follow(target, payload)

    assert target.read_text() == payload


@pytest.mark.skipif(not _SUPPORTS_DIR_FD, reason="dir_fd-anchored writes unsupported")
def test_download_refuses_dir_swapped_to_symlink_after_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # Simulate the TOCTOU race the dir fd closes: the slug dir passes the
    # writability check but is a symlink by the time we open it to write. The
    # no-follow directory fd must reject it so writes can't escape the archive.
    blogs = tmp_path / "blogs"
    blogs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (blogs / "fresh-post").symlink_to(outside)

    # Pretend the swap happened after the check by forcing it to pass.
    def always_writable(_target: Path) -> bool:
        return True

    monkeypatch.setattr(
        "src.sanity.blog_download._is_writable_target",
        always_writable,
    )

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig())

    assert result.written == 0
    assert result.skipped == 1
    # Nothing was written into the symlink target.
    assert list(outside.iterdir()) == []
    assert (blogs / "fresh-post").is_symlink()


def test_download_refuses_symlinked_leaf_in_owned_dir(tmp_path: Path):
    # A marked snapshot dir whose post.json was swapped for a symlink must not be
    # written through on rerun.
    blogs = tmp_path / "blogs"
    owned = _make_snapshot(blogs / "fresh-post")
    (owned / "post.json").unlink()
    target_outside = tmp_path / "secret.txt"
    target_outside.write_text("do not touch", encoding="utf-8")
    (owned / "post.json").symlink_to(target_outside)

    with patch(
        "src.sanity.blog_download.fetch_blog_posts_raw",
        return_value=[_raw("fresh-post")],
    ):
        result = download_blog_posts(tmp_path, config=SanityConfig())

    assert result.written == 0
    assert result.skipped == 1
    assert target_outside.read_text() == "do not touch"
