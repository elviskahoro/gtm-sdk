"""Tests for Exa search with mocked SDK."""

from unittest.mock import MagicMock, patch

from libs.exa.models import (
    ContentsOptions,
    SearchInput,
    SearchResponse,
)
from libs.exa.search import search


def test_search_basic():
    """Test basic search with mocked SDK."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.request_id = "req-123"
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = None
    mock_response.cost_dollars = 0.05

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search.return_value = mock_response

        input_q = SearchInput(query="test query", type="auto")
        result = search(input_q)

        assert isinstance(result, SearchResponse)
        assert result.request_id == "req-123"
        assert result.search_type == "auto"
        assert result.cost_dollars == 0.05
        assert result.results == []


def test_contents_false_routes_to_plain_search():
    """Regression (roborev): ``contents=False`` is a valid 'no contents' signal
    and must NOT trigger the more expensive ``search_and_contents`` path.
    The ``contents`` kwarg is also stripped before calling ``client.search()``
    since the plain endpoint does not accept it.
    """
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.request_id = "req-123"
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = None
    mock_response.cost_dollars = 0.0

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search.return_value = mock_response
        search(SearchInput(query="x", contents=False))

    mock_client.search.assert_called_once()
    mock_client.search_and_contents.assert_not_called()
    # Verify ``contents`` kwarg is stripped — the plain search endpoint doesn't
    # accept it (roborev finding).
    forwarded_kwargs = mock_client.search.call_args.kwargs
    assert "contents" not in forwarded_kwargs


def test_sdk_http_error_translated_to_typed_exa_error():
    """Regression (roborev): SDK exceptions with HTTP status get translated
    into the typed ExaError hierarchy so callers can branch on auth /
    rate-limit / server cleanly."""
    from libs.exa.errors import ExaRateLimitError

    class _FakeSDKError(Exception):
        def __init__(self):
            super().__init__("rate limited")
            self.status_code = 429
            self.request_id = "req_abc"

    mock_client = MagicMock()
    mock_client.search.side_effect = _FakeSDKError()

    with patch("libs.exa.search._get_client", return_value=mock_client):
        try:
            search(SearchInput(query="x"))
        except ExaRateLimitError as exc:
            assert exc.status == 429
            assert exc.request_id == "req_abc"
        else:
            raise AssertionError("expected ExaRateLimitError to be raised")


def test_sdk_error_with_status_on_exc_still_reads_response_metadata():
    """Regression (roborev): when status is on the exception itself, we must
    still consult ``exc.response`` for request_id/body so the typed error
    retains diagnostic context."""
    from libs.exa.errors import ExaRateLimitError

    class _FakeResponse:
        headers = {"x-request-id": "req_from_response"}

        def json(self):
            return {"message": "rate limited by upstream"}

    class _FakeSDKError(Exception):
        def __init__(self):
            super().__init__("err")
            self.status_code = 429  # status on the exception
            self.response = _FakeResponse()  # request_id/body on the response

    mock_client = MagicMock()
    mock_client.search.side_effect = _FakeSDKError()

    with patch("libs.exa.search._get_client", return_value=mock_client):
        try:
            search(SearchInput(query="x"))
        except ExaRateLimitError as exc:
            assert exc.status == 429
            assert exc.request_id == "req_from_response"
            assert exc.body == {"message": "rate limited by upstream"}
        else:
            raise AssertionError("expected ExaRateLimitError")


def test_sdk_non_http_error_is_not_translated():
    """SDK exceptions without an HTTP status (e.g. connection errors) pass
    through unwrapped so we don't mask the original failure shape."""
    mock_client = MagicMock()
    mock_client.search.side_effect = RuntimeError("connection reset")

    with patch("libs.exa.search._get_client", return_value=mock_client):
        try:
            search(SearchInput(query="x"))
        except RuntimeError as exc:
            assert "connection reset" in str(exc)
        else:
            raise AssertionError("expected RuntimeError to propagate")


def test_empty_contents_options_routes_to_plain_search():
    """Regression (roborev): ``ContentsOptions()`` with all slots None is
    object-truthy, but represents "no contents requested" — must route to
    plain ``search()``, not the more expensive ``search_and_contents()``."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.request_id = None
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = None
    mock_response.cost_dollars = 0.0

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search.return_value = mock_response
        search(SearchInput(query="x", contents=ContentsOptions()))

    mock_client.search.assert_called_once()
    mock_client.search_and_contents.assert_not_called()


def test_output_content_preserves_list_value():
    """Regression (roborev): structured ``output_schema`` results can be any
    JSON value (string, dict, list, number, boolean). The adapter must not
    narrow to ``str | dict`` and silently drop other shapes."""
    mock_client = MagicMock()
    mock_output = MagicMock()
    mock_output.content = [{"name": "Acme", "domain": "acme.com"}]
    mock_output.grounding = []

    mock_response = MagicMock()
    mock_response.request_id = "req"
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = mock_output
    mock_response.cost_dollars = 0.005

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search_and_contents.return_value = mock_response
        result = search(
            SearchInput(
                query="x",
                output_schema={"type": "array", "items": {"type": "object"}},
            ),
        )

    assert result.output is not None
    assert result.output.content == [{"name": "Acme", "domain": "acme.com"}]


def test_output_content_preserves_string_value():
    """Plain-text ``output_schema`` (``{"type":"text","description":...}``)
    returns a string. Must survive the adapter."""
    mock_client = MagicMock()
    mock_output = MagicMock()
    mock_output.content = "Snowflake is a cloud data platform."
    mock_output.grounding = []

    mock_response = MagicMock()
    mock_response.request_id = "req"
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = mock_output
    mock_response.cost_dollars = 0.005

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search_and_contents.return_value = mock_response
        result = search(
            SearchInput(
                query="x",
                output_schema={"type": "text", "description": "Summary"},
            ),
        )

    assert result.output is not None
    assert result.output.content == "Snowflake is a cloud data platform."


def test_output_with_falsy_grounding_list_preserved():
    """Regression (roborev): the mapper used to drop ``output`` whenever
    ``output_obj.grounding`` was falsy (e.g. ``[]``). Now we use ``is not None``
    so an empty grounding list still produces a valid ``OutputGrounding`` with
    empty citations."""
    mock_client = MagicMock()
    mock_output = MagicMock()
    mock_output.content = {"answer": "yes"}
    mock_output.grounding = []  # falsy but valid

    mock_response = MagicMock()
    mock_response.request_id = "req"
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = mock_output
    mock_response.cost_dollars = 0.001

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search_and_contents.return_value = mock_response
        result = search(
            SearchInput(
                query="x",
                output_schema={"type": "object"},
            ),
        )

    assert result.output is not None
    assert result.output.content == {"answer": "yes"}
    assert result.output.grounding is not None
    assert result.output.grounding.citations == []


def test_output_schema_alone_routes_to_search_and_contents():
    """Regression (roborev, high severity): ``output_schema`` requires the
    ``search_and_contents`` endpoint — plain ``search()`` does NOT populate
    ``response.output``. Without this routing, structured-output callers
    (e.g. the company domain resolver) silently get empty results."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.request_id = None
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = None
    mock_response.cost_dollars = 0.0

    schema = {
        "type": "object",
        "required": ["domain"],
        "properties": {"domain": {"type": "string"}},
    }

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search_and_contents.return_value = mock_response
        search(SearchInput(query="x", output_schema=schema))

    mock_client.search_and_contents.assert_called_once()
    mock_client.search.assert_not_called()


def test_contents_options_with_false_slots_routes_to_plain_search():
    """Regression (roborev): ``ContentsOptions(highlights=False)`` is an
    explicit "this slot off" signal, not a contents request. Must route
    to plain ``search()``, not the more expensive ``search_and_contents``."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.request_id = None
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = None
    mock_response.cost_dollars = 0.0

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search.return_value = mock_response
        search(SearchInput(query="x", contents=ContentsOptions(highlights=False)))

    mock_client.search.assert_called_once()
    mock_client.search_and_contents.assert_not_called()
    # ``contents`` kwarg stripped from the forwarded call.
    assert "contents" not in mock_client.search.call_args.kwargs


def test_contents_options_with_one_slot_routes_to_search_and_contents():
    """Conversely: a ``ContentsOptions`` with at least one slot set IS a
    contents request and must use ``search_and_contents``."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.request_id = None
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = None
    mock_response.cost_dollars = 0.0

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search_and_contents.return_value = mock_response
        search(SearchInput(query="x", contents=ContentsOptions(highlights=True)))

    mock_client.search_and_contents.assert_called_once()
    mock_client.search.assert_not_called()


def test_contents_true_routes_to_search_and_contents():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.request_id = "req-123"
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = None
    mock_response.cost_dollars = 0.0

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search_and_contents.return_value = mock_response
        search(SearchInput(query="x", contents=True))

    mock_client.search_and_contents.assert_called_once()
    mock_client.search.assert_not_called()


def test_search_with_results():
    """Test search with multiple results."""
    mock_client = MagicMock()
    mock_response = MagicMock()

    # Mock result items
    mock_result1 = MagicMock()
    mock_result1.url = "https://example.com"
    mock_result1.title = "Example Site"
    mock_result1.published_date = "2025-05-27"
    mock_result1.author = "John Doe"
    mock_result1.image = None
    mock_result1.favicon = None
    mock_result1.text = "Some text"
    mock_result1.highlights = ["highlight1"]
    mock_result1.highlight_scores = [0.9]
    mock_result1.summary = "A summary"
    mock_result1.subpages = None
    mock_result1.extras = None
    mock_result1.id = "id-1"

    mock_response.request_id = "req-456"
    mock_response.search_type = "fast"
    mock_response.results = [mock_result1]
    mock_response.output = None
    mock_response.cost_dollars = 0.03

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search.return_value = mock_response

        input_q = SearchInput(query="test", type="fast", num_results=5)
        result = search(input_q)

        assert len(result.results) == 1
        assert result.results[0].url == "https://example.com"
        assert result.results[0].title == "Example Site"
        assert result.cost_dollars == 0.03


def test_search_with_contents():
    """Test search_and_contents is called when contents is set."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.request_id = "req-789"
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = None
    mock_response.cost_dollars = 0.1

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search_and_contents.return_value = mock_response

        # Contents set means search_and_contents should be called
        input_q = SearchInput(
            query="test",
            contents=ContentsOptions(text=True),
        )
        result = search(input_q)

        # Verify search_and_contents was called, not search
        mock_client.search_and_contents.assert_called_once()
        mock_client.search.assert_not_called()
        assert result.cost_dollars == 0.1


def test_search_with_structured_output():
    """Test search with outputSchema and structured content."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.request_id = "req-output"
    mock_response.search_type = "auto"
    mock_response.results = []

    # Mock the Exa SDK response output object (raw API response)
    mock_output = MagicMock()
    mock_output.content = {"domain": "example.com", "confidence": "high"}

    # Mock a grounding citation from the Exa SDK
    mock_citation = MagicMock()
    mock_citation.url = "https://example.com"
    mock_citation.title = "Example"
    mock_citation.confidence = "high"
    mock_citation.published_date = None
    mock_citation.author = None
    mock_citation.text = None

    # grounding is a list of citation objects from the SDK
    mock_output.grounding = [mock_citation]

    mock_response.output = mock_output
    mock_response.cost_dollars = 0.15

    with patch("libs.exa.search._get_client", return_value=mock_client):
        # ``output_schema`` routes through search_and_contents() (the plain
        # search endpoint does not populate ``response.output``).
        mock_client.search_and_contents.return_value = mock_response

        input_q = SearchInput(
            query="domain for example",
            category="company",
            output_schema={
                "type": "object",
                "properties": {"domain": {"type": "string"}},
            },
        )
        result = search(input_q)

        assert result.output is not None
        assert result.output.content == {"domain": "example.com", "confidence": "high"}
        assert result.output.grounding is not None
        assert len(result.output.grounding.citations) == 1
        assert result.output.grounding.citations[0].url == "https://example.com"


def test_search_with_string_output():
    """Test search with string-type output content."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.request_id = "req-str"
    mock_response.search_type = "auto"
    mock_response.results = []

    # Mock output with string content
    mock_output = MagicMock()
    mock_output.content = "Some summary text"
    mock_output.grounding = None

    mock_response.output = mock_output
    mock_response.cost_dollars = 0.05

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search.return_value = mock_response

        input_q = SearchInput(query="test")
        result = search(input_q)

        assert result.output is not None
        assert result.output.content == "Some summary text"


def test_search_request_dict_format():
    """Test that search input is converted to snake_case request dict."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.request_id = "req-dict"
    mock_response.search_type = "auto"
    mock_response.results = []
    mock_response.output = None
    mock_response.cost_dollars = 0.0

    with patch("libs.exa.search._get_client", return_value=mock_client):
        mock_client.search.return_value = mock_response

        input_q = SearchInput(
            query="test",
            num_results=10,
            include_domains=["example.com"],
            exclude_domains=["spam.com"],
        )
        search(input_q)

        # Verify the call was made with snake_case keys
        call_args = mock_client.search.call_args
        assert call_args is not None
        called_dict = call_args[1]  # kwargs
        assert "query" in called_dict
        assert "num_results" in called_dict
        assert "include_domains" in called_dict
        assert "exclude_domains" in called_dict
        # Deprecated keys should NOT be present
        assert "use_autoprompt" not in called_dict
