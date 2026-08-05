"""Exa API adapter — typed wrapper around Exa SDK."""

from .client import ExaAPIKeyMissingError, api_key_scope
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
    "ExaAPIKeyMissingError",
    "ExaAuthError",
    "ExaBadRequestError",
    "ExaError",
    "ExaRateLimitError",
    "ExaServerError",
    "SearchInput",
    "SearchResponse",
    "api_key_scope",
    "find_companies",
    "find_people",
    "search",
]
