"""Exa find_companies Modal wrapper."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from libs.exa.client import ExaAPIKeyMissingError
from libs.exa.companies import find_companies
from libs.exa.errors import ExaError
from libs.exa.models import SearchResponse
from src.api_keys import inject_api_keys
from src.app import app, image
from src.secrets_bootstrap import bootstrap_secret, with_secrets


@app.function(image=image, secrets=[bootstrap_secret()])
@with_secrets("EXA_API_KEY")
def exa_find_companies(
    payload: dict[str, Any],
    api_keys: dict[str, str] | None = None,
) -> SearchResponse:
    """Find companies by query via Modal.

    Args:
        payload: Dict matching FindCompaniesQuery shape.
        api_keys: Optional API key overrides (passed by test/CLI).

    Returns:
        SearchResponse with company results and cost.
    """
    with inject_api_keys(api_keys or {}):
        try:
            query = FindCompaniesQuery.model_validate(payload)
        except ValidationError as exc:
            # Normalize to ValueError so the wrapper surface is consistent
            # with ``exa_search`` and Pydantic tracebacks don't leak across
            # the remote Modal boundary (roborev finding).
            raise ValueError(f"Invalid Exa payload: {exc}") from exc
        try:
            return find_companies(
                query.query,
                num_results=query.num_results,
                include_highlights=query.include_highlights,
                output_schema=query.output_schema,
            )
        except ExaError:
            # Typed Exa errors carry status + request_id; propagate untouched.
            raise
        except ExaAPIKeyMissingError as exc:
            raise ValueError(
                f"{exc!s} Set EXA_API_KEY in Infisical — the bootstrap "
                "pattern fetches it at function entry.",
            ) from None
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"{type(exc).__name__}: {exc}") from None


class FindCompaniesQuery(BaseModel):
    """Boundary model for the ``exa_find_companies`` Modal function.

    Validators mirror ``libs.exa.models.SearchInput`` so ``--json`` payloads
    fail at the wrapper, not deep inside a remote Modal call.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    num_results: int = Field(default=5, ge=1, le=100)
    include_highlights: bool = True
    output_schema: dict[str, Any] | None = None

    @field_validator("query")
    @classmethod
    def _strip_and_reject_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("query must be a non-empty / non-whitespace string")
        return stripped
