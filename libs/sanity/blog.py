"""Fetch dlthub ``blog.post`` documents via the Sanity Query API."""

from __future__ import annotations

from typing import Any

from .client import SanityConfig, query
from .errors import SanityQueryError
from .models import BlogPost

# GROQ projection that flattens the post into the shape BlogPost expects:
# - slug/title/description are lifted out of the nested `metadata` object
# - author/category references are dereferenced to their display fields
# - each Portable Text `image` block gets its asset URL resolved inline so the
#   markdown renderer can emit a real ![](url) without a second round-trip
BLOG_POST_GROQ = """
*[_type == "blog.post"]{
  _id,
  _createdAt,
  _updatedAt,
  publishDate,
  "slug": metadata.slug.current,
  "title": metadata.title,
  "description": metadata.description,
  "authors": authors[]->{_id, name},
  "categories": categories[]->{_id, title},
  body[]{
    ...,
    _type == "image" => {"url": asset->url}
  }
} | order(publishDate desc)
""".strip()


def fetch_blog_posts_raw(
    config: SanityConfig,
    *,
    allow_env_token: bool = False,
) -> list[dict[str, Any]]:
    """Fetch every blog post as the raw (flattened) query payload.

    Returns the projected dicts exactly as Sanity returned them — nothing is
    dropped or round-tripped through a model — so callers archiving the source
    of truth (e.g. ``post.json``) keep every field the projection selects.

    ``allow_env_token`` is forwarded to :func:`query`; it defaults to ``False``
    so a public dataset is read without an ambient ``SANITY_API_TOKEN``. Pass
    ``True`` to opt into the env fallback.

    The corpus is small (~200 docs) so a single query returns everything; add
    slice/cursor pagination here if it ever outgrows one response.
    """
    rows: Any = query(BLOG_POST_GROQ, config=config, allow_env_token=allow_env_token)
    if not isinstance(rows, list):
        # A non-list result means the query shape or API contract changed;
        # fail loudly rather than reporting a successful empty download.
        raise SanityQueryError(
            f"Expected a list of blog posts, got {type(rows).__name__}: "
            f"{str(rows)[:200]}",
        )

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            # Don't silently drop a malformed row — an unexpected element type
            # signals a broken response, not a row to skip.
            raise SanityQueryError(
                f"Expected a blog post object at index {index}, got "
                f"{type(row).__name__}: {str(row)[:200]}",
            )
    return rows


def fetch_blog_posts(
    config: SanityConfig,
    *,
    allow_env_token: bool = False,
) -> list[BlogPost]:
    """Fetch every blog post, validated into :class:`BlogPost` models."""
    return [
        BlogPost.model_validate(row)
        for row in fetch_blog_posts_raw(config, allow_env_token=allow_env_token)
    ]
