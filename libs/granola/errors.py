class GranolaError(Exception):
    """Base Granola domain error."""


class ConfigError(GranolaError):
    """Raised for invalid/missing configuration."""


class SchemaError(GranolaError):
    """Raised when source payload shape is unexpected."""


class SourceReadError(GranolaError):
    """Raised for source read/parse failures."""


class RateLimitError(GranolaError):
    """Raised when source API returns rate limit responses."""


class WriteError(GranolaError):
    """Raised when writing exports fails."""
