"""classify_error sanitizes pydantic ValidationError instead of leaking internals (ai-8k7).

A pydantic ``ValidationError`` is not one of the custom ``AttioError`` subclasses,
so it used to fall through to ``unknown_error`` and ``str(error)`` — which embeds the
``errors.pydantic.dev/...`` URL and the ``extra_forbidden``/``literal_error`` type
tags. The CLI integration tests (tests/cli/test_validation_error.py) assert those
tokens never reach the user-facing envelope, so classify_error must bucket pydantic
errors as a clean ``validation_error``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from libs.attio.errors import classify_error


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str


def _make_extra_forbidden_error() -> ValidationError:
    try:
        _StrictModel.model_validate({"email": "a@b.com", "country_code": "US"})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def test_pydantic_validation_error_classifies_as_validation_error() -> None:
    classified = classify_error(_make_extra_forbidden_error())
    assert classified.code == "validation_error"
    assert classified.fatal is True
    # The offending field is surfaced both in the message and as structured
    # metadata (so downstream ErrorEntry consumers keep the path).
    assert "country_code" in classified.message
    assert classified.field == "country_code"


def test_pydantic_validation_error_message_does_not_leak_internals() -> None:
    classified = classify_error(_make_extra_forbidden_error())
    lowered = classified.message.lower()
    assert "pydantic" not in lowered
    assert "extra_forbidden" not in lowered
    assert "literal_error" not in lowered


class _ScopeWordModel(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _reject(cls, value: str) -> str:
        # Error text deliberately contains a scope substring (_SCOPE_ERROR_SUBSTRINGS).
        raise ValueError("you do not have permission")


def test_pydantic_error_with_scope_substring_classifies_as_validation_error() -> None:
    # The pydantic branch must run BEFORE the _looks_like_scope_error heuristic, so a
    # validation message that happens to contain a scope substring is still a clean
    # validation_error, not a misfiled insufficient_scope.
    try:
        _ScopeWordModel(name="x")
    except ValidationError as exc:
        assert classify_error(exc).code == "validation_error"
        return
    raise AssertionError("expected a ValidationError")


def test_pydantic_error_wrapped_in_cause_is_still_sanitized() -> None:
    # A model_validate failure re-raised wrapped (pydantic error in __cause__)
    # must still be classified, not fall through to the leaky unknown_error path.
    pydantic_exc = _make_extra_forbidden_error()
    try:
        raise RuntimeError("outer wrapper") from pydantic_exc
    except RuntimeError as wrapped:
        classified = classify_error(wrapped)
    assert classified.code == "validation_error"
    assert classified.field == "country_code"
    assert "pydantic" not in classified.message.lower()
