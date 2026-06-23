from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError as PydanticValidationError

from libs.attio.sdk_boundary import describe_attio_error
from src.modal_app import MODAL_APP


class AttioError(Exception):
    pass


class ConfigurationError(AttioError):
    pass


class ConnectivityError(AttioError):
    pass


class SchemaMismatchError(AttioError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class ValidationError(AttioError):
    pass


class ConflictError(AttioError):
    def __init__(self, message: str, *, existing_record_id: str | None = None) -> None:
        super().__init__(message)
        self.existing_record_id = existing_record_id


class AttioAuthError(AttioError):
    pass


class AttioScopeError(AttioAuthError):
    """The token authenticated but lacks an OAuth scope a path requires.

    Distinct from :class:`AttioAuthError` (no/invalid token): here the token is
    valid and active, but its ``/v2/self`` scope set is missing a grant the
    operation needs (e.g. ``object_configuration:read-write`` to seed a new
    select option). Raised by the ``libs.attio.preflight`` scope check so the
    failure is legible at the orchestration entrypoint instead of surfacing as
    an opaque "...does not exist or you do not have permission..." error deep
    inside a write. See ai-ica.
    """

    def __init__(self, message: str, *, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []


class AttioNotFoundError(AttioError):
    pass


class DeploymentMismatchError(AttioError):
    pass


class AttioConflictError(ConflictError):
    pass


class AttioValidationError(ValidationError):
    pass


@dataclass
class ClassifiedError:
    code: str
    message: str
    error_type: str
    fatal: bool
    field: str | None = None


def translate_modal_signature_error(error: Exception) -> Exception:
    if isinstance(error, TypeError) and "unexpected keyword argument" in str(error):
        return DeploymentMismatchError(
            "Modal function signature mismatch. Deployed function is stale for current CLI payload. "
            f"Redeploy with: modal app stop {MODAL_APP} && modal deploy deploy.py --name {MODAL_APP}",
        )
    return error


# Substrings Attio returns when a token's scope (or per-object grant) is
# insufficient for the attempted write. The "...does not exist or you do not
# have permission..." wording is the exact opaque message that made ai-ica hard
# to diagnose: a missing-scope POST (e.g. seeding a select option without
# `object_configuration:read-write`) looks identical to a genuine 404. We bucket
# it as `insufficient_scope` so the envelope carries an actionable remediation
# instead of `unknown_error`.
_SCOPE_ERROR_SUBSTRINGS: tuple[str, ...] = (
    "do not have permission to access it",
    "do not have permission",
    "insufficient scope",
    "missing the required scope",
    "is not authorized",
)

_SCOPE_REMEDIATION = (
    "The Attio token authenticated but lacks the scope/permission for this "
    "write. If this is a select-option or attribute seed, the token needs "
    "`object_configuration:read-write` (or pre-bootstrap the schema with "
    "scripts/attio-bootstrap-tracking_events.py). Verify the token's scopes "
    "with GET /v2/self."
)


# Attio error codes the SDK's per-endpoint ``Code`` Literal omits, mapped to our
# ClassifiedError buckets. The SDK raises ResponseValidationError before the body
# is visible, so without re-parsing (see sdk_boundary.describe_attio_error) these
# all degraded to a generic pydantic "Invalid input for: ..." that masked the real
# message (e.g. value_not_found: "Cannot find attribute ... industry_select"). An
# unmapped code falls through to the real Attio code verbatim — truthful beats a
# normalized lie. See ai-e7s.
_ATTIO_CODE_TO_CLASSIFIED: dict[str, str] = {
    "value_not_found": "not_found",
    "uniqueness_conflict": "conflict",
    "unknown_filter_attribute_slug": "schema_mismatch",
}


def _looks_like_scope_error(error: Exception) -> bool:
    status = getattr(
        getattr(error, "raw_response", None),
        "status_code",
        None,
    )
    message = str(error).lower()
    if status == 403:
        return True
    return any(sub in message for sub in _SCOPE_ERROR_SUBSTRINGS)


def classify_error(error: Exception, *, strict: bool = False) -> ClassifiedError:
    if isinstance(error, AttioScopeError):
        return ClassifiedError(
            code="insufficient_scope",
            message=f"{error} {_SCOPE_REMEDIATION}",
            error_type=type(error).__name__,
            fatal=True,
        )
    if isinstance(error, DeploymentMismatchError):
        return ClassifiedError(
            code="modal_signature_mismatch",
            message=str(error),
            error_type=type(error).__name__,
            fatal=True,
        )
    if isinstance(error, ConfigurationError):
        return ClassifiedError(
            code="configuration_error",
            message=str(error),
            error_type=type(error).__name__,
            fatal=True,
        )
    if isinstance(error, ConnectivityError):
        return ClassifiedError(
            code="connectivity_error",
            message=str(error),
            error_type=type(error).__name__,
            fatal=True,
        )
    if isinstance(error, SchemaMismatchError):
        return ClassifiedError(
            code="schema_mismatch",
            message=str(error),
            error_type=type(error).__name__,
            fatal=strict,
            field=error.field,
        )
    if isinstance(error, ConflictError):
        return ClassifiedError(
            code="conflict",
            message=str(error),
            error_type=type(error).__name__,
            fatal=True,
        )
    if isinstance(error, (ValidationError, AttioValidationError)):
        return ClassifiedError(
            code="validation_error",
            message=str(error),
            error_type=type(error).__name__,
            fatal=True,
        )
    if isinstance(error, AttioNotFoundError):
        # Deterministic: the endpoint or workspace feature does not exist, so a
        # retry can't help. The meetings path (libs/attio/meetings.py) raises
        # this for a 404 on POST /v2/meetings — Attio's ALPHA meetings feature
        # is not provisioned in the dev workspace. See ai-h5y.
        return ClassifiedError(
            code="not_found",
            message=str(error),
            error_type=type(error).__name__,
            fatal=True,
        )

    # The SDK raises ResponseValidationError (with a pydantic ValidationError in
    # `__cause__`) for any Attio code its narrow `Code` Literal omits. Re-parse the
    # body BEFORE the pydantic-unwrap branch below so the real status/code/message
    # wins over the generic "Invalid input for: ...". A genuine pydantic error on
    # our OWN request input has no `.body`, so describe_attio_error returns None and
    # the pydantic branch still handles it (no regression). See ai-e7s.
    attio_desc = describe_attio_error(error)
    if attio_desc is not None and attio_desc.code is not None:
        return ClassifiedError(
            code=_ATTIO_CODE_TO_CLASSIFIED.get(attio_desc.code, attio_desc.code),
            message=attio_desc.message or str(error),
            error_type=type(error).__name__,
            fatal=True,
        )

    # pydantic's own ValidationError is NOT one of the custom AttioError subclasses
    # above, so without this branch it fell through to `unknown_error` and
    # `str(error)` leaked pydantic internals — the `errors.pydantic.dev/...` URL and
    # the `extra_forbidden`/`literal_error` type tags — into the user-facing
    # envelope. Also unwrap one level of `__cause__`: a model_validate failure is
    # sometimes re-raised wrapped (e.g. a ResponseValidationError) with the pydantic
    # error chained, which would otherwise slip back into the leaky path. This runs
    # BEFORE the `_looks_like_scope_error` heuristic so a validation message that
    # happens to contain a scope substring isn't misfiled as insufficient_scope. (ai-8k7)
    pydantic_exc = (
        error
        if isinstance(error, PydanticValidationError)
        else error.__cause__
        if isinstance(error.__cause__, PydanticValidationError)
        else None
    )
    if pydantic_exc is not None:
        field_paths = [
            ".".join(str(part) for part in e["loc"])
            for e in pydantic_exc.errors()
            if e.get("loc")
        ]
        return ClassifiedError(
            code="validation_error",
            # Message lists every offending location; `field` carries a single
            # canonical path (the first), matching how `field` is a singular path
            # everywhere else (e.g. SchemaMismatchError) rather than an ambiguous
            # comma-joined pseudo-field. (ai-8k7)
            message=f"Invalid input for: {', '.join(field_paths) or 'request'}",
            error_type=type(pydantic_exc).__name__,
            fatal=True,
            field=field_paths[0] if field_paths else None,
        )

    if _looks_like_scope_error(error):
        return ClassifiedError(
            code="insufficient_scope",
            message=f"{error} {_SCOPE_REMEDIATION}",
            error_type=type(error).__name__,
            fatal=True,
        )

    return ClassifiedError(
        code="unknown_error",
        message=str(error),
        error_type=type(error).__name__,
        fatal=True,
    )
