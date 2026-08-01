# ruff: noqa: S101 -- asserts are the point of a test file.
# pyright: reportPrivateUsage=none
# Mirrors tests/libs/parallel/test_client.py — exercises the module-private
# ``_get_client`` to verify the api_key resolution chain (explicit arg >
# api_key_scope contextvar > LINEAR_API_KEY env var) and contextvar reset/nest
# semantics.
from __future__ import annotations

import sys
import types
from typing import ClassVar

import pytest
from gtm_linear import PaginationOrderBy

# The window CI triage reads back when looking for an open ticket to bump.
TRIAGE_ISSUE_WINDOW = 100


class _CapturingFakeLinear:
    """Light stand-in that records the api_key _get_client passed."""

    last_api_key: str | None = None

    def __init__(self, *, api_key: str, **_kwargs: object) -> None:
        type(self).last_api_key = api_key


@pytest.fixture
def _fake_sdk(monkeypatch) -> type[_CapturingFakeLinear]:  # pyright: ignore[reportUnusedFunction]
    _CapturingFakeLinear.last_api_key = None
    fake_module = types.ModuleType("gtm_linear")
    fake_module.LinearClient = _CapturingFakeLinear  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gtm_linear", fake_module)
    return _CapturingFakeLinear


def test_get_client_raises_without_api_key(monkeypatch, _fake_sdk) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    from libs.linear.client import _get_client

    with pytest.raises(ValueError):
        _get_client()


def test_get_client_uses_env_var(monkeypatch, _fake_sdk) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "from-env")
    from libs.linear.client import _get_client

    _get_client()
    assert _fake_sdk.last_api_key == "from-env"


def test_get_client_uses_explicit_api_key_over_everything(
    monkeypatch,
    _fake_sdk,
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "from-env")
    from libs.linear.client import _get_client, api_key_scope

    with api_key_scope("from-scope"):
        _get_client(api_key="from-arg")
    assert _fake_sdk.last_api_key == "from-arg"


def test_get_client_uses_scope_over_env(monkeypatch, _fake_sdk) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "from-env")
    from libs.linear.client import _get_client, api_key_scope

    with api_key_scope("from-scope"):
        _get_client()
    assert _fake_sdk.last_api_key == "from-scope"


def test_api_key_scope_resets_on_exit(monkeypatch, _fake_sdk) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    from libs.linear.client import _get_client, api_key_scope

    with api_key_scope("inside"):
        _get_client()
        assert _fake_sdk.last_api_key == "inside"
    with pytest.raises(ValueError):
        _get_client()


# --- happy-path adapter surface ---


class _FakeAsyncClient:
    """Async-context-manager stand-in for ``gtm_linear.LinearClient``."""

    last_api_key: str | None = None

    def __init__(self, *, api_key: str, **_kwargs: object) -> None:
        type(self).last_api_key = api_key

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _install_async_sdk(monkeypatch, queries_cls, mutations_cls) -> None:
    fake = types.ModuleType("gtm_linear")
    fake.LinearClient = _FakeAsyncClient  # type: ignore[attr-defined]
    fake.LinearQueries = queries_cls  # type: ignore[attr-defined]
    fake.LinearMutations = mutations_cls  # type: ignore[attr-defined]
    # Real enum: the adapter must pass the member the SDK actually accepts, and
    # 0.1.0 renamed these from UPPER_SNAKE to lowerCamel.
    fake.PaginationOrderBy = PaginationOrderBy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gtm_linear", fake)


def test_get_issue_async_invokes_queries(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class _Queries:
        def __init__(self, client: object) -> None:
            seen["client"] = client

        async def get_issue(self, issue_id: str) -> str:
            seen["issue_id"] = issue_id
            return "issue-payload"

    class _Mutations:
        def __init__(self, _client: object) -> None: ...

    _install_async_sdk(monkeypatch, _Queries, _Mutations)
    monkeypatch.setenv("LINEAR_API_KEY", "k")

    from libs.linear.client import get_issue

    result = get_issue("ISS-1")
    assert result == "issue-payload"
    assert seen["issue_id"] == "ISS-1"
    assert _FakeAsyncClient.last_api_key == "k"


def test_create_issue_async_invokes_mutations(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class _Queries:
        def __init__(self, _client: object) -> None: ...

    class _Mutations:
        def __init__(self, client: object) -> None:
            seen["client"] = client

        async def create_issue(self, payload: object) -> str:
            seen["payload"] = payload
            return "created-issue"

    _install_async_sdk(monkeypatch, _Queries, _Mutations)
    monkeypatch.setenv("LINEAR_API_KEY", "k")

    from libs.linear.client import create_issue

    result = create_issue("input-sentinel")  # type: ignore[arg-type]
    assert result == "created-issue"
    assert seen["payload"] == "input-sentinel"


def test_get_team_by_key_invokes_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutations only take team UUIDs, so key -> UUID is its own round-trip."""
    seen: dict[str, object] = {}

    class _Queries:
        def __init__(self, _client: object) -> None: ...

        async def get_team_by_key(self, key: str) -> str:
            seen["key"] = key
            return "team-payload"

    class _Mutations:
        def __init__(self, _client: object) -> None: ...

    _install_async_sdk(monkeypatch, _Queries, _Mutations)
    monkeypatch.setenv("LINEAR_API_KEY", "k")

    from libs.linear.client import get_team_by_key

    assert get_team_by_key("AI") == "team-payload"
    assert seen["key"] == "AI"


def test_list_open_team_issues_filters_and_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI-triage dedupe query.

    Every part of this is load-bearing: without the state filter a closed
    triage ticket keeps absorbing new failures, and without the ordering
    "newest open issue" is whatever Linear felt like returning.
    """
    seen: dict[str, object] = {}

    class _Page:
        nodes: ClassVar[list[str]] = ["issue-a", "issue-b"]

    class _Queries:
        def __init__(self, _client: object) -> None: ...

        async def list_issues_page(
            self,
            filter_: object,
            first: int,
            order_by: object,
        ) -> _Page:
            seen["filter"] = filter_
            seen["first"] = first
            seen["order_by"] = order_by
            return _Page()

    class _Mutations:
        def __init__(self, _client: object) -> None: ...

    _install_async_sdk(monkeypatch, _Queries, _Mutations)
    monkeypatch.setenv("LINEAR_API_KEY", "k")

    from libs.linear.client import list_open_team_issues

    assert list_open_team_issues("team-uuid") == ["issue-a", "issue-b"]
    assert seen["filter"] == {
        "team": {"id": {"eq": "team-uuid"}},
        "state": {"type": {"nin": ["completed", "canceled"]}},
    }
    assert seen["first"] == TRIAGE_ISSUE_WINDOW, "the window CI triage relies on"
    assert seen["order_by"] is PaginationOrderBy.updatedAt


def test_api_key_scope_nests(monkeypatch, _fake_sdk) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    from libs.linear.client import _get_client, api_key_scope

    with api_key_scope("outer"):
        _get_client()
        assert _fake_sdk.last_api_key == "outer"
        with api_key_scope("inner"):
            _get_client()
            assert _fake_sdk.last_api_key == "inner"
        _get_client()
        assert _fake_sdk.last_api_key == "outer"
