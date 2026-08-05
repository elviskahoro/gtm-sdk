"""Sanity Query API adapter — typed wrapper around the GROQ Query API."""

from .blog import BLOG_POST_GROQ, fetch_blog_posts_raw
from .client import (
    DEFAULT_API_VERSION,
    DEFAULT_DATASET,
    DEFAULT_PROJECT_ID,
    SanityConfig,
    api_key_scope,
    query,
)
from .errors import (
    DuplicateSlugError,
    SanityError,
    SanityQueryError,
    UnsafeArchiveDirError,
)
from .models import Author, BlogPost, Category
from .portable_text import escape_text, escape_trailing_atx, to_markdown

__all__ = [
    "BLOG_POST_GROQ",
    "DEFAULT_API_VERSION",
    "DEFAULT_DATASET",
    "DEFAULT_PROJECT_ID",
    "Author",
    "BlogPost",
    "Category",
    "DuplicateSlugError",
    "SanityConfig",
    "SanityError",
    "SanityQueryError",
    "UnsafeArchiveDirError",
    "api_key_scope",
    "escape_text",
    "escape_trailing_atx",
    "fetch_blog_posts_raw",
    "query",
    "to_markdown",
]
