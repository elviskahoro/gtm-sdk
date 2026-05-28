"""Exa convenience wrapper for people search."""

from __future__ import annotations

from .models import ContentsOptions, HighlightsOptions, SearchInput, SearchResponse
from .search import search


def find_people(
    query: str,
    *,
    num_results: int = 5,
    include_highlights: bool = True,
    api_key: str | None = None,
) -> SearchResponse:
    """Search for people by query using Exa.

    Pins ``category="people"`` and ``type="auto"``.

    Args:
        query: Search query (e.g., "CEO of Anthropic").
        num_results: Number of results to return (default 5).
        include_highlights: Whether to include highlights in results.
        api_key: Optional API key override.

    Returns:
        SearchResponse with typed results and cost information.
    """
    contents_arg = None
    if include_highlights:
        contents_arg = ContentsOptions(highlights=HighlightsOptions())

    input = SearchInput(
        query=query,
        type="auto",
        category="people",
        num_results=num_results,
        contents=contents_arg,
    )
    return search(input, api_key=api_key)
