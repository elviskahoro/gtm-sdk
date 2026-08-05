# ruff: noqa: INP001 -- Bazel test packages intentionally mirror source layout.
"""Verify the dynamic edge facade carries its adapter runtime dependencies."""

from src.edge import query


def test_edge_facade_resolves_motherduck_query() -> None:
    """Importing a late facade export must include DuckDB in Bazel runfiles."""
    if not callable(query):
        message = "src.edge.query must resolve to a callable"
        raise TypeError(message)
