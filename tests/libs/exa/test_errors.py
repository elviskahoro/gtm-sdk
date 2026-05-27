"""Tests for Exa error classes and HTTP status mapping."""

from libs.exa.errors import (
    ExaAuthError,
    ExaBadRequestError,
    ExaRateLimitError,
    ExaServerError,
    from_http_status,
)


def test_from_http_status_401():
    """Test 401 Unauthorized maps to ExaAuthError."""
    error = from_http_status(
        401,
        body={"message": "Invalid API key"},
        request_id="req-123",
    )
    assert isinstance(error, ExaAuthError)
    assert error.status == 401
    assert error.request_id == "req-123"
    assert "Invalid API key" in str(error)


def test_from_http_status_400():
    """Test 400 Bad Request maps to ExaBadRequestError."""
    error = from_http_status(
        400,
        body={"message": "Invalid parameters"},
        request_id="req-456",
    )
    assert isinstance(error, ExaBadRequestError)
    assert error.status == 400
    assert error.request_id == "req-456"


def test_from_http_status_422():
    """Test 422 Unprocessable Entity maps to ExaBadRequestError."""
    error = from_http_status(
        422,
        body={"message": "Schema validation failed"},
        request_id="req-789",
    )
    assert isinstance(error, ExaBadRequestError)
    assert error.status == 422


def test_from_http_status_429():
    """Test 429 Too Many Requests maps to ExaRateLimitError."""
    error = from_http_status(
        429,
        body={"message": "Rate limit exceeded"},
        request_id="req-limit",
    )
    assert isinstance(error, ExaRateLimitError)
    assert error.status == 429


def test_from_http_status_5xx():
    """Test 5xx Server Error maps to ExaServerError."""
    for status in [500, 502, 503, 504]:
        error = from_http_status(status, request_id=f"req-{status}")
        assert isinstance(error, ExaServerError)
        assert error.status == status


def test_from_http_status_missing_message():
    """Test from_http_status with missing message falls back to default."""
    error = from_http_status(400, body=None)
    assert isinstance(error, ExaBadRequestError)
    assert "Bad Request" in str(error)


def test_from_http_status_unknown():
    """Test from_http_status with unknown status code."""
    error = from_http_status(418, request_id="req-teapot")
    # Should return base ExaError for unknown codes
    assert error.status == 418


def test_from_http_status_tolerates_non_dict_body():
    """Regression (roborev): real-world error responses sometimes return a
    non-dict body (a string, list, or null). ``from_http_status`` must not
    crash on these — that would mask the original HTTP failure with an
    ``AttributeError`` from ``body.get(...)``."""
    # String body
    err1 = from_http_status(429, body="rate limited as text")  # type: ignore[arg-type]
    assert err1.status == 429

    # List body
    err2 = from_http_status(400, body=["error", "details"])  # type: ignore[arg-type]
    assert err2.status == 400

    # The original body is still preserved on the exception for downstream
    # inspection — only the message-extraction normalizes shape.
    assert err1.body == "rate limited as text"
    assert err2.body == ["error", "details"]
