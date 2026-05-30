from __future__ import annotations

import os
from typing import Any

import modal
import pytest

from src.modal_app import MODAL_APP

pytestmark = pytest.mark.integration

# MODAL_ENVIRONMENT is intentionally not gated here: no call site passes
# environment_name=, so the Modal client resolves the environment from the token's
# default workspace environment. Requiring it would only force CI to inject a value
# the client already infers.
REQUIRED_ENV_VARS = [
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "PARALLEL_API_KEY",
]


def _missing_env_vars() -> list[str]:
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]


def _skip_if_missing_env() -> None:
    missing = _missing_env_vars()
    if missing:
        pytest.skip(
            "Remote smoke tests gated by credentials preflight failure in this module",
        )


# Markers that identify a Parallel.ai "out of credit" rejection. The API returns
# HTTP 402 with a billing message when the account balance is exhausted. Match on
# the status code plus a billing keyword so a genuine 4xx with "402" in some other
# field can't masquerade as a credit problem.
_PARALLEL_CREDIT_MARKERS = ("insufficient credit", "billing")


def _remote_or_skip_on_parallel_credit(fn: Any, /, **kwargs: Any) -> Any:
    """Invoke a Parallel-backed Modal function, skipping on an out-of-credit 402.

    The three Parallel smoke tests exercise the live Parallel.ai API. When the
    account runs out of credit the API returns HTTP 402, which surfaced as a hard
    test failure and was the *actual* source of the "flaky" nightly runs this
    module was filed for (ai-8k7): credit present → green, credit depleted → red.

    Treat it like the missing-credential preflight above — skip, loudly — so a
    billing condition outside the code's control doesn't redden CI. Top up at
    https://platform.parallel.ai/settings?tab=billing to restore live coverage.
    Modal re-raises the remote error as varied local types (ValueError from the
    parallel_search wrapper, ExecutionError when the SDK's APIStatusError can't be
    deserialized locally), so match on the stringified message rather than a type.
    """
    try:
        return fn.remote(**kwargs)  # pyrefly: ignore[invalid-param-spec]
    except Exception as exc:  # noqa: BLE001 — see docstring: type varies, match message
        msg = str(exc).lower()
        if "402" in msg and any(marker in msg for marker in _PARALLEL_CREDIT_MARKERS):
            pytest.skip(
                "Parallel API returned HTTP 402 (insufficient credit) — top up at "
                "https://platform.parallel.ai/settings?tab=billing to restore coverage",
            )
        raise


# --- Metadata / Capability Tests ---


def test_gtm_remote_metadata_attio_people_runtime() -> None:
    _skip_if_missing_env()
    fn = modal.Function.from_name(MODAL_APP, "attio_people_runtime_metadata")
    payload = fn.remote()  # pyrefly: ignore[invalid-param-spec]
    assert isinstance(payload, dict)
    assert payload.get("app") == MODAL_APP
    assert "build_git_sha" in payload
    assert "deployed_at" in payload
    capabilities = payload.get("capabilities", {})
    assert capabilities.get("attio_people_upsert.additional_emails") is True


# --- Research and Search Tests ---


def test_gtm_remote_smoke_non_mutating_research() -> None:
    _skip_if_missing_env()
    fn = modal.Function.from_name(MODAL_APP, "gtm_research")
    response = _remote_or_skip_on_parallel_credit(
        fn,
        payload={"objective": "acme ai startup"},
        api_keys={"parallel_api_key": os.environ["PARALLEL_API_KEY"]},
    )
    payload = response.model_dump() if hasattr(response, "model_dump") else response
    assert isinstance(payload, dict)
    assert payload.get("objective") == "acme ai startup"
    results = payload.get("results")
    assert isinstance(results, list)
    # Tightened beyond a type check: an empty list passed silently and let a
    # broken research path masquerade as green. A live search for this objective
    # must return at least one result, each a dict. (ai-8k7)
    assert results, "gtm_research returned no results"
    assert all(isinstance(item, dict) for item in results)


def test_gtm_remote_smoke_enrich() -> None:
    _skip_if_missing_env()
    fn = modal.Function.from_name(MODAL_APP, "gtm_enrich")
    response = _remote_or_skip_on_parallel_credit(
        fn,
        payload={"url": "https://example.com", "objective": "company info"},
        api_keys={"parallel_api_key": os.environ["PARALLEL_API_KEY"]},
    )
    payload = response.model_dump() if hasattr(response, "model_dump") else response
    assert isinstance(payload, dict)
    assert payload.get("url") == "https://example.com"
    assert payload.get("objective") == "company info"
    # `data` is a dumped Parallel ExtractResponse — {extract_id, result, errors}.
    # Require a successful result, not just extract_id: a failed extract returns
    # result=None with populated errors, and asserting extract_id alone would let
    # that broken path look healthy. example.com is a stable always-up page, so a
    # missing result here is a real regression, not live-data noise. The URL lives
    # at result["url"]; we don't assert an exact match (Parallel may normalize it)
    # nor non-empty excerpts (example.com legitimately yields sparse ones). (ai-8k7)
    data = payload.get("data")
    assert isinstance(data, dict) and data, "gtm_enrich returned empty data"
    assert "extract_id" in data
    result = data.get("result")
    assert isinstance(result, dict) and result.get("url"), (
        f"gtm_enrich extract did not succeed (errors={data.get('errors')})"
    )


def test_gtm_remote_parallel_search() -> None:
    _skip_if_missing_env()
    fn = modal.Function.from_name(MODAL_APP, "parallel_search")
    response = _remote_or_skip_on_parallel_credit(
        fn,
        payload={"objective": "python web framework best practices", "max_results": 5},
        api_keys={"parallel_api_key": os.environ["PARALLEL_API_KEY"]},
    )
    payload = response.model_dump() if hasattr(response, "model_dump") else response
    assert isinstance(payload, dict)
    # Tightened: a non-empty search_id string and at least one result, instead of
    # only checking the key exists / is a list. (ai-8k7)
    search_id = payload.get("search_id")
    assert isinstance(search_id, str) and search_id, (
        "parallel_search returned no search_id"
    )
    results = payload.get("results")
    assert isinstance(results, list)
    assert results, "parallel_search returned no results"


# --- Attio People Search (Read-Only) ---


def test_gtm_remote_attio_search_people_by_email() -> None:
    _skip_if_missing_env()
    fn = modal.Function.from_name(MODAL_APP, "attio_search_people")
    response = fn.remote(
        payload={"email": "nonexistent-smoke-test@example.com"},
        api_keys={"attio_api_key": os.environ.get("ATTIO_API_KEY", "")},
    )
    payload = response.model_dump() if hasattr(response, "model_dump") else response
    assert isinstance(payload, dict)
    assert "success" in payload
    # May return empty results for nonexistent email; just validate structure
    assert "record_id" in payload or "warnings" in payload or "errors" in payload


def test_gtm_remote_attio_search_people_by_domain() -> None:
    _skip_if_missing_env()
    fn = modal.Function.from_name(MODAL_APP, "attio_search_people")
    response = fn.remote(
        payload={"email_domain": "example.com", "limit": 5},
        api_keys={"attio_api_key": os.environ.get("ATTIO_API_KEY", "")},
    )
    payload = response.model_dump() if hasattr(response, "model_dump") else response
    assert isinstance(payload, dict)
    assert "success" in payload


# --- Batch Operations (Preview Mode) ---


def test_gtm_remote_smoke_mutating_preview_batch_people() -> None:
    _skip_if_missing_env()
    fn = modal.Function.from_name(MODAL_APP, "gtm_batch_add_people")
    response = fn.remote(
        payload={
            "records": [{"email": "remote-smoke@example.com"}],
            "apply": False,
        },
        api_keys={"attio_api_key": os.environ.get("ATTIO_API_KEY", "")},
    )
    payload = response.model_dump() if hasattr(response, "model_dump") else response
    assert isinstance(payload, dict)
    assert payload.get("mode") == "preview"
    assert payload.get("created") == 0
    assert isinstance(payload.get("requested"), int)


def test_gtm_remote_smoke_mutating_preview_batch_companies() -> None:
    _skip_if_missing_env()
    fn = modal.Function.from_name(MODAL_APP, "gtm_batch_add_companies")
    response = fn.remote(
        payload={
            "records": [{"domain": "example.com"}],
            "apply": False,
        },
        api_keys={"attio_api_key": os.environ.get("ATTIO_API_KEY", "")},
    )
    payload = response.model_dump() if hasattr(response, "model_dump") else response
    assert isinstance(payload, dict)
    assert payload.get("mode") == "preview"
    assert isinstance(payload.get("requested"), int)
