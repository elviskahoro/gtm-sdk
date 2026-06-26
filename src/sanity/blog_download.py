"""Download every dlthub blog post to ``<out_dir>/blogs/<slug>/``.

Each post folder gets two files:

- ``post.json`` — the untouched query payload (every field the projection
  returns, not just the modeled ones) for full-fidelity re-processing.
- ``index.md`` — YAML frontmatter + the Portable Text body rendered to Markdown.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from libs.sanity.client import SanityConfig
from libs.sanity.blog import fetch_blog_posts_raw
from libs.sanity.errors import DuplicateSlugError, UnsafeArchiveDirError
from libs.sanity.models import BlogPost
from libs.sanity.portable_text import escape_text, escape_trailing_atx, to_markdown

BLOG_BASE_URL = "https://dlthub.com/blog"

# Fail an open() if the final path component is a symlink (no-follow). Absent on
# platforms that lack it (e.g. Windows), where it degrades to a plain open.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# Require the opened path to be a directory (ENOTDIR otherwise). 0 where absent.
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
# Whether os.open() accepts a dir_fd, letting us anchor leaf writes to a real
# directory inode. False on Windows, where we fall back to path-based writes.
_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd

# Written into every snapshot directory so pruning only ever deletes folders
# this tool created — a user's own ``blogs/<dir>/`` (even one that happens to
# contain a ``post.json``) is never removed.
_SNAPSHOT_MARKER = ".gtm-sanity-snapshot"


# Characters illegal in a path segment on at least one mainstream OS: the
# separators, the Windows-reserved set, and C0 control codes (incl. NUL).
_UNSAFE_SLUG_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows reserved device names (matched case-insensitively, with or without an
# extension), which are invalid filenames even though they contain no illegal
# characters.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)},
)


def is_safe_slug(slug: str) -> bool:
    """Reject slugs that are not a single, portable, safe path segment.

    Slugs come from untrusted CMS content and are joined into a filesystem
    path. Beyond traversal (``/``, ``\\``, ``.``/``..``), this also rejects
    characters and reserved names that are invalid on at least one mainstream
    OS, so a bad slug fails the planning pass rather than blowing up mid-write
    on someone else's platform.
    """
    if slug in {"", ".", ".."}:
        return False
    if _UNSAFE_SLUG_CHARS.search(slug):
        return False
    # Windows silently strips a trailing dot/space, aliasing the name to another.
    if slug[-1] in {".", " "}:
        return False
    # Windows trims trailing spaces/dots from the stem before matching a reserved
    # device name, so do the same here or ``con .txt`` would slip through.
    stem = slug.split(".", 1)[0].rstrip(" .").casefold()
    return stem not in _WINDOWS_RESERVED_NAMES


def _slug_key(slug: str) -> str:
    """Canonical key for slug identity across filesystems.

    NFC-normalizes (so Unicode-equivalent forms like NFD ``café`` and NFC
    ``café`` collapse) then case-folds (for case-insensitive volumes), matching
    how a normalization-/case-insensitive filesystem decides two names are the
    same directory.
    """
    return unicodedata.normalize("NFC", slug).casefold()


@dataclass
class DownloadResult:
    """Summary of a blog download run."""

    out_dir: Path
    written: int = 0
    skipped: int = 0
    pruned: int = 0
    slugs: list[str] = field(default_factory=list)


def _frontmatter(post: BlogPost, slug: str) -> str:
    """Serialize the post's metadata as a YAML frontmatter block.

    ``slug`` is the NFC-normalized value used for the on-disk directory; it is
    used for both the ``slug`` field and the ``source`` URL so the link matches
    the folder rather than diverging on a differently-normalized raw slug.

    Uses ``yaml.safe_dump`` rather than hand-rolled quoting so multiline
    titles/descriptions and other control characters are emitted as valid YAML
    (block scalars / proper escaping) and ``index.md`` stays parseable.
    """
    data: dict[str, object] = {
        "title": post.title or "",
        "slug": slug,
    }
    if post.publish_date:
        data["publishDate"] = post.publish_date
    if post.description:
        data["description"] = post.description
    data["authors"] = [a.name for a in post.authors if a.name]
    data["categories"] = [c.title for c in post.categories if c.title]
    # Percent-encode the slug: it's interpolated into a URL, and a slug with a
    # space or reserved character would otherwise yield a malformed link.
    data["source"] = f"{BLOG_BASE_URL}/{quote(slug, safe='')}"

    dumped = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    return f"---\n{dumped}---"


def _render_markdown(post: BlogPost, slug: str) -> str:
    parts = [_frontmatter(post, slug)]
    if post.title:
        # Escape and collapse newlines: the title is CMS-sourced and emitted as
        # a raw H1, so metacharacters/HTML would otherwise alter structure and a
        # newline would split the heading.
        heading = escape_text(post.title).replace("\r", " ").replace("\n", " ")
        # A trailing run of ``#`` is parsed as an ATX heading-close sequence and
        # stripped (``# Release #`` -> "Release"); backslash it to keep the hash.
        heading = escape_trailing_atx(heading)
        parts.append(f"# {heading}")
    body = to_markdown(post.body)
    if body:
        parts.append(body)
    return "\n\n".join(parts) + "\n"


def _write_no_follow(path: Path, data: str, *, dir_fd: int | None = None) -> None:
    """Write ``data`` to ``path``, refusing to follow a symlink at the leaf.

    Uses ``O_NOFOLLOW`` so an attacker-planted leaf symlink raises ``OSError``
    (atomic, no TOCTOU window) rather than redirecting the write elsewhere. When
    ``dir_fd`` is given, ``path`` is resolved relative to that open directory
    descriptor so the *parent* directory can't be swapped between validation and
    write either (``O_NOFOLLOW`` only guards the final component).

    ``os.write`` may persist fewer bytes than requested, so the encoded payload
    is written in a loop until the whole buffer lands — otherwise a large
    ``post.json`` / ``index.md`` could be silently truncated.
    """
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW,
        0o644,
        dir_fd=dir_fd,
    )
    try:
        buf = data.encode("utf-8")
        view = memoryview(buf)
        written = 0
        while written < len(buf):
            written += os.write(fd, view[written:])
    finally:
        os.close(fd)


def _write_leaf(target: Path, name: str, data: str, dir_fd: int | None) -> None:
    """Write a snapshot leaf file, anchored to ``dir_fd`` when available.

    With a directory fd, the name is resolved relative to that descriptor; without
    one (platforms lacking ``dir_fd`` support) it falls back to the absolute path.
    """
    path = Path(name) if dir_fd is not None else target / name
    _write_no_follow(path, data, dir_fd=dir_fd)


def _is_writable_target(target: Path) -> bool:
    """Whether a snapshot may be written to ``target`` without clobbering data.

    Safe when the path does not yet exist (we create it) or is a real directory
    we previously created (carries the marker) whose leaf files are real files.
    A symlinked directory, a plain file, a pre-existing directory the user owns,
    or a marked directory with a symlinked leaf (``post.json`` / ``index.md`` /
    marker) is refused so a rerun can't follow an attacker-planted symlink into
    an arbitrary write.
    """
    # Check the symlink first: ``exists()`` follows the link, so a broken symlink
    # would otherwise look absent and be treated as writable.
    if target.is_symlink():
        return False
    if not target.exists():
        return True
    if not target.is_dir():
        return False
    if not (target / _SNAPSHOT_MARKER).is_file():
        return False
    # A marked dir is ours, but its leaves could have been swapped for symlinks.
    return not any(
        (target / leaf).is_symlink()
        for leaf in ("post.json", "index.md", _SNAPSHOT_MARKER)
    )


def _prepare_blogs_dir(blogs_dir: Path) -> None:
    """Ensure ``blogs/`` is a real directory before any per-slug write.

    The per-slug checks in ``_is_writable_target`` only guard the leaf, so a
    symlinked or non-directory ``blogs/`` parent would slip past them: a
    plain file there crashes the run with ``NotADirectoryError`` on the first
    ``mkdir(parents=True)``, and a symlinked ``blogs/`` would redirect every
    snapshot write into another tree. Validate it here, create it if absent, and
    re-check after creation so a symlink swapped in during the mkdir (TOCTOU)
    is still caught.
    """
    if blogs_dir.is_symlink() or (blogs_dir.exists() and not blogs_dir.is_dir()):
        raise UnsafeArchiveDirError(
            f"{blogs_dir} exists but is not a real directory (symlink or file); "
            f"refusing to write the archive through it.",
        )
    blogs_dir.mkdir(parents=True, exist_ok=True)
    if blogs_dir.is_symlink():
        raise UnsafeArchiveDirError(
            f"{blogs_dir} became a symlink during creation; refusing to follow it.",
        )


def _prune_stale(blogs_dir: Path, live_slugs: set[str]) -> int:
    """Remove ``blogs/<slug>`` snapshots no longer present in the live corpus.

    Scoped deliberately: only directories *directly* under ``blogs/`` that we
    recognize as our own snapshots (they carry the ``_SNAPSHOT_MARKER`` file this
    tool writes) and whose slug is absent from the latest download are removed,
    so unrelated directories the user may have placed in ``out/`` — even ones
    that happen to contain a ``post.json`` — are never touched.

    Slug comparison is canonicalized (NFC + case-fold) to match how collisions
    are detected: on a normalization-/case-insensitive filesystem a slug that
    changed only in casing or Unicode form maps to the same directory as before,
    so a naive comparison would wrongly treat the still-live snapshot as stale
    and delete it.
    """
    if not blogs_dir.is_dir():
        return 0

    live = {_slug_key(slug) for slug in live_slugs}
    pruned = 0
    for child in blogs_dir.iterdir():
        if (
            child.is_dir()
            # Never follow/delete a symlinked dir: it's not one we created.
            and not child.is_symlink()
            and _slug_key(child.name) not in live
            and (child / _SNAPSHOT_MARKER).is_file()
        ):
            shutil.rmtree(child)
            pruned += 1
    return pruned


def download_blog_posts(
    out_dir: Path,
    *,
    config: SanityConfig,
    prune: bool = True,
    allow_env_token: bool = False,
) -> DownloadResult:
    """Fetch all posts and write them under ``out_dir/blogs/<slug>/``.

    When ``prune`` is true (the default) snapshots of posts that have since been
    deleted or renamed in Sanity are removed so the local archive mirrors the
    live corpus instead of accumulating stale directories.

    ``allow_env_token`` is forwarded to the fetch layer; it defaults to
    ``False`` so a public dataset is read without picking up an ambient
    ``SANITY_API_TOKEN``. Pass ``True`` to opt into the env fallback.
    """
    # Canonicalize the archive root once: resolving collapses any symlinks in the
    # caller-supplied path (including symlinked ancestors) to a real location, so
    # writes and prunes operate on the same concrete tree and can't diverge.
    # Within that tree, per-slug leaf checks and O_NOFOLLOW writes keep the tool
    # from following or clobbering symlinks it didn't create.
    out_dir = out_dir.resolve()
    blogs_dir = out_dir / "blogs"

    raw_posts = fetch_blog_posts_raw(config, allow_env_token=allow_env_token)

    # Every slug present in the live corpus, even ones we skip writing this run.
    # Pruning keys off this set, not the written set, so a post that still
    # exists in Sanity but is skipped (e.g. a transiently malformed slug) never
    # has its existing snapshot deleted.
    present_slugs: set[str] = set()

    # Canonical slug (NFC + case-fold) -> (slug, _id) of the post that first
    # claimed it. Canonicalizing catches slugs that are distinct in Sanity but
    # alias to the same directory on a normalization-/case-insensitive
    # filesystem (macOS: `Foo` vs `foo`, NFD vs NFC `café`). Collisions are
    # resolved in this planning pass, *before* any file is written, so a late
    # duplicate aborts the run instead of leaving a partially updated archive.
    slug_owner: dict[str, tuple[str, str | None]] = {}
    planned: list[tuple[BlogPost, dict[str, Any], str]] = []

    result = DownloadResult(out_dir=out_dir)
    for raw in raw_posts:
        # Validate for the rendered view and slug, but archive the untouched
        # query payload so post.json keeps every field, not just modeled ones.
        post = BlogPost.model_validate(raw)
        # NFC-normalize so the on-disk directory name is stable regardless of the
        # Unicode form Sanity returns.
        slug = unicodedata.normalize("NFC", post.slug) if post.slug else None
        if slug:
            present_slugs.add(slug)
        if not slug or not is_safe_slug(slug):
            result.skipped += 1
            continue

        key = _slug_key(slug)
        if key in slug_owner:
            first_slug, first_id = slug_owner[key]
            raise DuplicateSlugError(
                f"Two blog posts map to the same output path: _id {first_id!r} "
                f"(slug {first_slug!r}) and _id {post.id!r} (slug {slug!r}); "
                f"the second would overwrite the first. Disambiguate the slug in "
                f"Sanity before archiving.",
            )
        slug_owner[key] = (slug, post.id)
        planned.append((post, raw, slug))

    # Collisions ruled out; only now is it safe to touch the filesystem. Validate
    # (and create) the shared ``blogs/`` parent once before the per-slug loop so a
    # symlinked or non-directory parent is caught up front rather than crashing or
    # redirecting the first write.
    _prepare_blogs_dir(blogs_dir)
    for post, raw, slug in planned:
        target = blogs_dir / slug
        if not _is_writable_target(target):
            # A pre-existing path we don't own (user dir, symlink, file): refuse
            # to clobber it. Count as skipped, which also disables pruning.
            result.skipped += 1
            continue
        target.mkdir(parents=True, exist_ok=True)

        # Open the snapshot directory itself with O_NOFOLLOW and anchor every leaf
        # write to that descriptor. _is_writable_target() checked the slug dir, but
        # that check races the write: if target is swapped to a symlink afterward,
        # O_NOFOLLOW on the leaf alone wouldn't stop the parent component from
        # redirecting the write outside the archive tree. Binding to the real
        # inode via the dir fd closes that window. (No dir_fd support -> Windows
        # best-effort path-based writes, matching the O_NOFOLLOW degradation.)
        dir_fd: int | None = None
        if _SUPPORTS_DIR_FD:
            try:
                dir_fd = os.open(target, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
            except OSError:
                # target became a symlink (or vanished) after the writability
                # check; refuse to write through it.
                result.skipped += 1
                continue

        try:
            # Write the ownership marker first so a snapshot interrupted partway
            # (write error below, or the process killed) still carries proof this
            # tool created it. Otherwise the marker-last order would leave a
            # markerless directory that _is_writable_target treats as unowned,
            # turning the post into a permanent un-repairable orphan.
            _write_leaf(
                target,
                _SNAPSHOT_MARKER,
                "Created by `gtm sanity blog download`; safe to delete.\n",
                dir_fd,
            )
            _write_leaf(
                target,
                "post.json",
                json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
                dir_fd,
            )
            _write_leaf(target, "index.md", _render_markdown(post, slug), dir_fd)
        except OSError:
            # A leaf was a symlink (O_NOFOLLOW) or otherwise unwritable; refuse to
            # follow it. Remove the partial snapshot we just started so a rerun
            # can recreate it cleanly rather than tripping over a half-written
            # directory, then skip the post (which also disables pruning).
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            result.skipped += 1
            continue
        finally:
            if dir_fd is not None:
                os.close(dir_fd)

        result.written += 1
        result.slugs.append(slug)

    # Prune only when every live post mapped to a safe output path. A skipped
    # post (unsafe/missing slug this run) may have an existing snapshot stored
    # under a previous, valid slug; since we can't know that slug, pruning could
    # delete the last good archive of a still-live post over a transient CMS
    # glitch. Skipping the prune is the safe failure mode.
    if prune and result.skipped == 0:
        result.pruned = _prune_stale(blogs_dir, present_slugs)

    return result
