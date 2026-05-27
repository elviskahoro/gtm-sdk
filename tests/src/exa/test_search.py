"""Tests for src/exa/* Modal wrappers (Pattern A passthrough).

Mirrors tests/src/parallel/test_modal_wrappers.py: validate the payload-validation
+ libs.exa.search call shape using fn.local() to invoke Modal-decorated functions.
"""

from __future__ import annotations

from typing import cast
import importlib

import modal
import pytest
from pydantic import ValidationError

from libs.exa.client import ExaAPIKeyMissingError
from libs.exa.errors import ExaAuthError, ExaRateLimitError
from libs.exa.models import SearchInput, SearchResponse
from src.exa.companies import FindCompaniesQuery, exa_find_companies
from src.exa.people import FindPeopleQuery, exa_find_people
from src.exa.search import SearchQuery, exa_search


def _stub_response() -> SearchResponse:
    return SearchResponse(
        request_id="req_x",
        search_type="auto",
        results=[],
        output=None,
        cost_dollars=0.001,
    )


def test_search_query_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SearchQuery.model_validate({"query": "x", "useAutoprompt": True})


def test_exa_search_rejects_invalid_num_results_at_boundary(monkeypatch) -> None:
    called = False

    def fake_search(_input):
        nonlocal called
        called = True
        return _stub_response()

    monkeypatch.setattr("src.exa.search.search", fake_search)

    fn = cast(modal.Function, exa_search)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="num_results"):
        fn.local(
            payload={"query": "snowflake", "num_results": 0},
            api_keys={"exa_api_key": "exa_test"},
        )

    assert called is False


def test_find_companies_query_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        FindCompaniesQuery.model_validate({"query": "x", "category": "company"})


def test_find_people_query_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        FindPeopleQuery.model_validate({"query": "x", "category": "people"})


def test_exa_search_passes_payload_to_libs_search(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_search(input_):
        captured["input"] = input_
        return _stub_response()

    monkeypatch.setattr("src.exa.search.search", fake_search)

    fn = cast(modal.Function, exa_search)  # type: ignore[arg-type]
    result = cast(
        SearchResponse,
        fn.local(
            payload={"query": "snowflake", "type": "auto", "num_results": 3},
            api_keys={"exa_api_key": "exa_test"},
        ),
    )

    assert isinstance(result, SearchResponse)
    assert captured["input"].query == "snowflake"  # type: ignore[union-attr]
    assert captured["input"].type == "auto"  # type: ignore[union-attr]
    assert captured["input"].num_results == 3  # type: ignore[union-attr]


def test_exa_search_honors_contents_false(monkeypatch) -> None:
    captured: dict[str, object] = {}
    search_module = importlib.import_module("libs.exa.search")

    def fake_search(**kwargs):
        captured["method"] = "search"
        captured["kwargs"] = kwargs
        return _stub_response()

    def fake_search_and_contents(**kwargs):
        captured["method"] = "search_and_contents"
        captured["kwargs"] = kwargs
        return _stub_response()

    def fake_get_client(_api_key: str | None = None):
        return type(
            "Client",
            (),
            {
                "search": staticmethod(fake_search),
                "search_and_contents": staticmethod(fake_search_and_contents),
            },
        )()

    monkeypatch.setattr(
        search_module,
        "_get_client",
        fake_get_client,
    )

    result = SearchQuery.model_validate(
        {"query": "snowflake", "contents": False},
    )
    from libs.exa.search import search as libs_search

    libs_search(result)

    assert captured["method"] == "search"
    assert "contents" not in captured["kwargs"]  # type: ignore[index]


def test_exa_find_companies_pins_category(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_find(query, **kwargs):
        captured["query"] = query
        captured["kwargs"] = kwargs
        return _stub_response()

    monkeypatch.setattr("src.exa.companies.find_companies", fake_find)

    fn = cast(modal.Function, exa_find_companies)  # type: ignore[arg-type]
    result = cast(
        SearchResponse,
        fn.local(
            payload={"query": "datadog", "num_results": 4},
            api_keys={"exa_api_key": "exa_test"},
        ),
    )

    assert isinstance(result, SearchResponse)
    assert captured["query"] == "datadog"
    assert captured["kwargs"]["num_results"] == 4  # type: ignore[index]


def test_exa_find_people_pins_category(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_find(query, **kwargs):
        captured["query"] = query
        captured["kwargs"] = kwargs
        return _stub_response()

    monkeypatch.setattr("src.exa.people.find_people", fake_find)

    fn = cast(modal.Function, exa_find_people)  # type: ignore[arg-type]
    result = cast(
        SearchResponse,
        fn.local(
            payload={"query": "olaf carlson-wee", "num_results": 2},
            api_keys={"exa_api_key": "exa_test"},
        ),
    )

    assert isinstance(result, SearchResponse)
    assert captured["query"] == "olaf carlson-wee"
    assert captured["kwargs"]["num_results"] == 2  # type: ignore[index]


def test_exa_search_decorates_missing_key_error(monkeypatch) -> None:
    """Missing EXA_API_KEY error gets decorated with Infisical hint."""

    def boom(_input):
        raise ValueError("EXA_API_KEY not found in environment")

    monkeypatch.setattr("src.exa.search.search", boom)

    fn = cast(modal.Function, exa_search)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Infisical"):
        fn.local(payload={"query": "x"}, api_keys={"exa_api_key": "exa_test"})


def test_empty_query_rejected_at_modal_boundary() -> None:
    """Regression (roborev): query='' must be rejected at the wrapper, not
    forwarded to the SDK where it would fail later with an opaque error."""
    with pytest.raises(ValidationError):
        SearchQuery.model_validate({"query": ""})


def test_whitespace_only_query_rejected() -> None:
    """Regression (roborev): ``query="   "`` is not a useful Exa input.
    The validator strips and rejects whitespace-only across all three
    boundary models (SearchQuery / FindCompaniesQuery / FindPeopleQuery)."""
    with pytest.raises(ValidationError, match="non-empty"):
        SearchQuery.model_validate({"query": "   "})
    with pytest.raises(ValidationError, match="non-empty"):
        FindCompaniesQuery.model_validate({"query": "\t\n"})
    with pytest.raises(ValidationError, match="non-empty"):
        FindPeopleQuery.model_validate({"query": " "})


def test_query_stripped_on_valid_input() -> None:
    """Valid queries are normalized — leading/trailing whitespace removed."""
    q = SearchQuery.model_validate({"query": "  snowflake  "})
    assert q.query == "snowflake"


def test_find_companies_empty_query_rejected() -> None:
    with pytest.raises(ValidationError):
        FindCompaniesQuery.model_validate({"query": "", "num_results": 5})


def test_find_companies_num_results_bounded() -> None:
    """Regression (roborev): ``FindCompaniesQuery`` must enforce the same
    1..100 ``num_results`` bound as the underlying SearchInput."""
    with pytest.raises(ValidationError):
        FindCompaniesQuery.model_validate({"query": "x", "num_results": 0})
    with pytest.raises(ValidationError):
        FindCompaniesQuery.model_validate({"query": "x", "num_results": 101})


def test_find_people_num_results_bounded() -> None:
    with pytest.raises(ValidationError):
        FindPeopleQuery.model_validate({"query": "x", "num_results": 0})


def test_api_key_missing_error_triggers_infisical_hint(monkeypatch) -> None:
    """Regression (roborev): the real ``_get_client`` raises
    ``ExaAPIKeyMissingError`` (not a plain ``ValueError``). The Modal
    wrapper must recognize that class explicitly and attach the Infisical
    remediation hint, independent of message wording."""
    err = ExaAPIKeyMissingError("Exa API key not resolved.")

    def _raise(_input: SearchInput) -> SearchResponse:
        raise err

    monkeypatch.setattr("src.exa.search.search", _raise)

    fn = cast(modal.Function, exa_search)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Infisical"):
        fn.local(payload={"query": "x"}, api_keys={"exa_api_key": "exa_test"})


def test_typed_exa_errors_propagate_through_modal_wrapper(monkeypatch) -> None:
    """Regression (roborev): typed ExaError subclasses carry status/request_id
    that callers need for retry decisions. They must NOT be wrapped as
    generic ValueError at the Modal boundary."""

    def raise_rate_limit(_input):
        raise ExaRateLimitError("rate limited", status=429, request_id="req_test")

    monkeypatch.setattr("src.exa.search.search", raise_rate_limit)

    fn = cast(modal.Function, exa_search)  # type: ignore[arg-type]
    with pytest.raises(ExaRateLimitError) as excinfo:
        fn.local(payload={"query": "x"}, api_keys={"exa_api_key": "exa_test"})

    assert excinfo.value.status == 429
    assert excinfo.value.request_id == "req_test"


def test_find_companies_propagates_typed_exa_errors(monkeypatch) -> None:
    def raise_auth(_query, **_kwargs):
        raise ExaAuthError("bad token", status=401, request_id="req_auth")

    monkeypatch.setattr("src.exa.companies.find_companies", raise_auth)

    fn = cast(modal.Function, exa_find_companies)  # type: ignore[arg-type]
    with pytest.raises(ExaAuthError) as excinfo:
        fn.local(payload={"query": "x"}, api_keys={"exa_api_key": "exa_test"})

    assert excinfo.value.status == 401
    assert excinfo.value.request_id == "req_auth"


def test_find_people_propagates_typed_exa_errors(monkeypatch) -> None:
    def raise_server(_query, **_kwargs):
        raise ExaRateLimitError("slow down", status=429, request_id="req_rl")

    monkeypatch.setattr("src.exa.people.find_people", raise_server)

    fn = cast(modal.Function, exa_find_people)  # type: ignore[arg-type]
    with pytest.raises(ExaRateLimitError) as excinfo:
        fn.local(payload={"query": "x"}, api_keys={"exa_api_key": "exa_test"})

    assert excinfo.value.status == 429
    assert excinfo.value.request_id == "req_rl"


def test_find_companies_validation_error_normalized_to_value_error() -> None:
    """Regression (roborev): bad ``--json`` payload must surface as a stable
    ValueError at the Modal boundary, not leak a Pydantic ValidationError
    traceback across the wire."""
    fn = cast(modal.Function, exa_find_companies)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Invalid Exa payload"):
        fn.local(payload={"query": "", "num_results": 5}, api_keys={"exa_api_key": "x"})


def test_find_people_validation_error_normalized_to_value_error() -> None:
    fn = cast(modal.Function, exa_find_people)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Invalid Exa payload"):
        fn.local(
            payload={"query": "x", "num_results": 0},
            api_keys={"exa_api_key": "x"},
        )


def test_typed_exa_auth_error_propagates_through_find_companies(monkeypatch) -> None:
    def raise_auth(_query, **_kw):
        raise ExaAuthError("bad token", status=401, request_id="req_auth")

    monkeypatch.setattr("src.exa.companies.find_companies", raise_auth)

    fn = cast(modal.Function, exa_find_companies)  # type: ignore[arg-type]
    with pytest.raises(ExaAuthError) as excinfo:
        fn.local(payload={"query": "x"}, api_keys={"exa_api_key": "exa_test"})

    assert excinfo.value.status == 401
