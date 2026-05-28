"""Exa search Modal wrapper."""

from typing import Any

from pydantic import ValidationError

from libs.exa.client import ExaAPIKeyMissingError
from libs.exa.errors import ExaError
from libs.exa.models import SearchInput, SearchResponse
from libs.exa.search import search
from src.api_keys import inject_api_keys
from src.app import app, image
from src.secrets_bootstrap import bootstrap_secret, with_secrets

# Reuse ``SearchInput`` as the Modal boundary model so payload validation at
# the wrapper matches the SDK call: ``Literal`` types on ``type`` and
# ``category``, ``num_results`` bounds, the category-conditional invariants.
# Previously a separate ``SearchQuery`` BaseModel was looser than ``SearchInput``
# and let malformed payloads through (roborev finding).
SearchQuery = SearchInput


def _decorate_exa_key_error(exc: ValueError) -> ValueError:
    """Attach an Infisical remediation hint to missing-key errors.

    Prefers an ``isinstance`` check against the dedicated
    :class:`ExaAPIKeyMissingError` so the hint is reliable regardless of
    the underlying error message wording. Falls back to a substring match
    for legacy ``ValueError`` shapes.
    """
    is_missing_key = (
        isinstance(exc, ExaAPIKeyMissingError)
        or "EXA API key not resolved"
        in str(
            exc,
        )
        or "EXA_API_KEY" in str(exc)
    )
    if not is_missing_key:
        return exc
    return ValueError(
        f"{exc!s} Set EXA_API_KEY in Infisical (env=dev|staging|prod) — "
        "the bootstrap pattern fetches it at function entry. See "
        "src/secrets_bootstrap.py.",
    )


@app.function(image=image, secrets=[bootstrap_secret()])
@with_secrets("EXA_API_KEY")
def exa_search(
    payload: dict[str, Any],
    api_keys: dict[str, str] | None = None,
) -> SearchResponse:
    """Execute Exa search via Modal.

    Args:
        payload: Dict matching SearchQuery shape (an alias for ``SearchInput``).
        api_keys: Optional API key overrides (passed by test/CLI).

    Returns:
        SearchResponse with results, structured output (if requested), and cost.

    Raises:
        ValueError: For ``ValidationError`` (malformed payload) and missing
            ``EXA_API_KEY``. ``ExaError`` subclasses (auth, rate limit, etc.)
            propagate untouched so callers can branch on them.
    """
    with inject_api_keys(api_keys or {}):
        try:
            query = SearchQuery.model_validate(payload)
        except ValidationError as exc:
            # Re-raise as ValueError so the wrapper surface is consistent —
            # callers don't need to catch both ValidationError and ValueError.
            raise ValueError(f"Invalid Exa payload: {exc}") from exc
        try:
            return search(query)
        except ExaError:
            # Typed Exa errors (auth, rate limit, bad request, server) carry
            # status + request_id context callers need for retry decisions —
            # let them propagate untouched.
            raise
        except ValueError as exc:
            raise _decorate_exa_key_error(exc) from None
        except TypeError:
            raise
        except Exception as exc:
            # Unexpected SDK error — wrap with type name so the operator can
            # see what blew up at the Modal boundary.
            raise ValueError(f"{type(exc).__name__}: {exc}") from None
