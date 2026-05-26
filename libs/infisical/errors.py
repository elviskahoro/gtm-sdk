class InfisicalAuthError(RuntimeError):
    """Bootstrap creds for Infisical are missing or invalid."""


class InfisicalFetchError(RuntimeError):
    """A named secret could not be retrieved from Infisical."""
