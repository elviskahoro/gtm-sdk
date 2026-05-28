"""Exa convenience wrapper for company search."""

from __future__ import annotations

from typing import Any

from .models import ContentsOptions, HighlightsOptions, SearchInput, SearchResponse
from .search import search


def find_companies(
    query: str,
    *,
    num_results: int = 5,
    include_highlights: bool = True,
    output_schema: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> SearchResponse:
    """Search for companies by query using Exa.

    Pins ``category="company"`` and ``type="auto"``. Other parameters are
    exposed for flexibility.

    Args:
        query: Search query (e.g., "primary website domain for Snowflake").
        num_results: Number of results to return (default 5).
        include_highlights: Whether to include highlights in results.
        output_schema: Structured output schema (dict).
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
        category="company",
        num_results=num_results,
        contents=contents_arg,
        output_schema=output_schema,
    )
    return search(input, api_key=api_key)
