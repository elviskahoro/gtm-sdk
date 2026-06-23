"""ai-e7s: an Attio 4xx whose code != 'missing_value' must surface its real
status/code/message instead of being masked by the SDK's ResponseValidationError.

The generated Attio client constrains ``body.code`` to a narrow per-endpoint
``Literal`` (e.g. ``missing_value``), so any other valid code fails response
unmarshalling — the SDK raises ``ResponseValidationError`` (pydantic
``ValidationError`` in ``__cause__``) and the underlying message is lost. We
re-parse the body via :func:`describe_attio_error` and feed the real fields into
:func:`classify_error`.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from libs.attio.errors import classify_error
from libs.attio.sdk_boundary import describe_attio_error


class _FakeResponseValidationError(Exception):
    """Mimics attio.errors.ResponseValidationError shape (has .body)."""

    def __init__(self, body: str) -> None:
        super().__init__("Response validation failed: pydantic noise here")
        self.body = body


# The exact prod envelope behind ai-3gx: a PATCH onto an archived attribute slug.
_VALUE_NOT_FOUND_BODY = (
    '{"status_code": 400, "type": "invalid_request_error",'
    ' "code": "value_not_found",'
    ' "message": "Cannot find attribute with slug/ID \\"industry_select\\"."}'
)


def _make_pydantic_error() -> ValidationError:
    class _M(BaseModel):
        code: str

    try:
        _M(code=123)  # type: ignore[arg-type]
    except ValidationError as exc:
        return exc
    msg = "expected a ValidationError"
    raise AssertionError(msg)


def test_describe_attio_error_surfaces_real_code_and_message() -> None:
    err = _FakeResponseValidationError(_VALUE_NOT_FOUND_BODY)
    err.__cause__ = _make_pydantic_error()

    desc = describe_attio_error(err)

    assert desc is not None
    assert desc.code == "value_not_found"
    assert desc.status_code == 400
    assert desc.type == "invalid_request_error"
    assert "Cannot find attribute" in (desc.message or "")
    assert "industry_select" in (desc.message or "")


def test_describe_attio_error_returns_none_for_bodyless_pydantic_error() -> None:
    # A genuine pydantic error on our own input has no `.body` — must not be
    # mistaken for an Attio envelope.
    assert describe_attio_error(_make_pydantic_error()) is None


def test_describe_attio_error_returns_none_for_non_attio_body() -> None:
    assert describe_attio_error(_FakeResponseValidationError("not json at all")) is None
    # JSON object but missing code/message -> not Attio's documented envelope.
    assert describe_attio_error(_FakeResponseValidationError('{"foo": "bar"}')) is None


def test_classify_error_surfaces_real_message_not_pydantic_literal() -> None:
    err = _FakeResponseValidationError(_VALUE_NOT_FOUND_BODY)
    err.__cause__ = _make_pydantic_error()

    classified = classify_error(err)

    # value_not_found is mapped to the not_found bucket, carrying the REAL message.
    assert classified.code == "not_found"
    assert "Cannot find attribute" in classified.message
    assert "industry_select" in classified.message
    # The pydantic noise must NOT leak through.
    assert "Input should be" not in classified.message
    assert "Invalid input for" not in classified.message


def test_classify_error_unmapped_attio_code_surfaces_verbatim() -> None:
    body = (
        '{"status_code": 429, "type": "rate_limit_error",'
        ' "code": "rate_limit_exceeded", "message": "Slow down."}'
    )
    err = _FakeResponseValidationError(body)
    err.__cause__ = _make_pydantic_error()

    classified = classify_error(err)

    assert classified.code == "rate_limit_exceeded"
    assert classified.message == "Slow down."


def test_classify_error_bodyless_pydantic_still_validation_error() -> None:
    # Regression guard for ai-8k7: a bodyless pydantic error must still classify
    # as validation_error via the unwrap branch, NOT be touched by the new path.
    classified = classify_error(_make_pydantic_error())
    assert classified.code == "validation_error"
    assert "pydantic" not in classified.message.lower()
