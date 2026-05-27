"""Exa API adapter — typed wrapper around Exa SDK."""

from .companies import find_companies
from .errors import (
    ExaAuthError,
    ExaBadRequestError,
    ExaError,
    ExaRateLimitError,
    ExaServerError,
)
from .models import SearchInput, SearchResponse
from .people import find_people
from .search import search

__all__ = [
    "search",
    "find_companies",
    "find_people",
    "SearchInput",
    "SearchResponse",
    "ExaError",
    "ExaAuthError",
    "ExaBadRequestError",
    "ExaRateLimitError",
    "ExaServerError",
]
