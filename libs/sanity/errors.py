"""Sanity API error classes."""

from __future__ import annotations


class SanityError(RuntimeError):
    """Base exception for Sanity API errors."""


class SanityQueryError(SanityError):
    """A GROQ query failed (non-2xx response or transport error)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DuplicateSlugError(SanityError):
    """Two documents in the corpus resolve to the same output slug.

    Snapshots are written to ``blogs/<slug>/``, so a collision would silently
    overwrite the first document with the second. We fail fast instead of
    dropping content from the archive.
    """


class UnsafeArchiveDirError(SanityError):
    """The ``blogs/`` parent under ``out_dir`` is not a real directory.

    All snapshots live under ``<out_dir>/blogs/``. If that path is a symlink or
    a plain file, ``mkdir(parents=True)`` would either follow the link (writing
    into a tree the tool doesn't own) or crash mid-run with ``NotADirectoryError``.
    We validate it up front and fail fast instead.
    """
