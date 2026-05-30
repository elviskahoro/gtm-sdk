from __future__ import annotations


class FathomError(Exception):
    """Base class for Fathom adapter errors."""


class FathomAuthError(FathomError):
    """Raised when no Fathom API key can be resolved."""
