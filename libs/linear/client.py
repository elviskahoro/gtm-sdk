"""Linear SDK client builder.

Thin wrapper around the ``gtm_linear`` package. Mirrors
:mod:`libs.parallel.client` — a single ``_get_client()`` factory whose key
resolution order is:

1. Explicit ``api_key`` argument.
2. The :func:`api_key_scope` contextvar — set by ``src/app.py`` and webhook
   entrypoints after fetching the key from Infisical.
3. ``os.environ["LINEAR_API_KEY"]`` — back-compat for legacy named Modal
   Secret callers.

The upstream SDK is async-first; helpers here expose both ``*_async`` and
sync variants. Sync variants drive the async client via :func:`asyncio.run`
so callers that aren't already inside an event loop stay ergonomic.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gtm_linear import (
        Issue,
        IssueCreateInput,
        IssueUpdateInput,
        LinearClient,
        Team,
        User,
    )


_api_key_var: ContextVar[str | None] = ContextVar(
    "linear_api_key",
    default=None,
)


@contextmanager
def api_key_scope(api_key: str) -> Generator[None, None, None]:
    """Bind ``api_key`` as the active Linear key for this async/sync context.

    Mirrors :func:`libs.parallel.client.api_key_scope`. Reset on exit so
    concurrent Modal inputs in the same container do not see each other's
    keys.
    """
    token = _api_key_var.set(api_key)
    try:
        yield
    finally:
        _api_key_var.reset(token)


def _resolve_key(api_key: str | None = None) -> str:
    token = (
        api_key or _api_key_var.get() or os.environ.get("LINEAR_API_KEY", "")
    ).strip()
    if not token:
        raise ValueError(
            "Linear API key not resolved. Provide one of: "
            "(1) explicit api_key= argument, "
            "(2) call inside libs.linear.client.api_key_scope(...), "
            "(3) set LINEAR_API_KEY in the process environment.",
        )
    return token


def _get_client(api_key: str | None = None) -> LinearClient:
    from gtm_linear import LinearClient

    return LinearClient(api_key=_resolve_key(api_key))


async def get_issue_async(
    issue_id: str,
    *,
    api_key: str | None = None,
) -> Issue | None:
    from gtm_linear import LinearQueries

    async with _get_client(api_key) as client:
        return await LinearQueries(client).get_issue(issue_id)


async def list_issues_async(
    team_id: str,
    *,
    first: int = 50,
    api_key: str | None = None,
) -> list[Issue]:
    from gtm_linear import LinearQueries

    async with _get_client(api_key) as client:
        return await LinearQueries(client).list_issues(team_id, first=first)


async def search_issues_async(
    term: str,
    *,
    api_key: str | None = None,
) -> list[Issue]:
    from gtm_linear import LinearQueries

    async with _get_client(api_key) as client:
        return await LinearQueries(client).search_issues(term)


async def get_team_async(
    team_id: str,
    *,
    api_key: str | None = None,
) -> Team | None:
    from gtm_linear import LinearQueries

    async with _get_client(api_key) as client:
        return await LinearQueries(client).get_team(team_id)


async def get_user_async(
    user_id: str,
    *,
    api_key: str | None = None,
) -> User | None:
    from gtm_linear import LinearQueries

    async with _get_client(api_key) as client:
        return await LinearQueries(client).get_user(user_id)


async def create_issue_async(
    input_: IssueCreateInput,
    *,
    api_key: str | None = None,
) -> Issue:
    from gtm_linear import LinearMutations

    async with _get_client(api_key) as client:
        return await LinearMutations(client).create_issue(input_)


async def update_issue_async(
    issue_id: str,
    update: IssueUpdateInput,
    *,
    api_key: str | None = None,
) -> Issue:
    from gtm_linear import LinearMutations

    async with _get_client(api_key) as client:
        return await LinearMutations(client).update_issue(issue_id, update)


# Sync convenience wrappers — only call from contexts not already inside a
# running event loop. They will raise RuntimeError if one is active.


def get_issue(issue_id: str, *, api_key: str | None = None) -> Issue | None:
    return asyncio.run(get_issue_async(issue_id, api_key=api_key))


def list_issues(
    team_id: str,
    *,
    first: int = 50,
    api_key: str | None = None,
) -> list[Issue]:
    return asyncio.run(list_issues_async(team_id, first=first, api_key=api_key))


def search_issues(term: str, *, api_key: str | None = None) -> list[Issue]:
    return asyncio.run(search_issues_async(term, api_key=api_key))


def get_team(team_id: str, *, api_key: str | None = None) -> Team | None:
    return asyncio.run(get_team_async(team_id, api_key=api_key))


def get_user(user_id: str, *, api_key: str | None = None) -> User | None:
    return asyncio.run(get_user_async(user_id, api_key=api_key))


def create_issue(
    input_: IssueCreateInput,
    *,
    api_key: str | None = None,
) -> Issue:
    return asyncio.run(create_issue_async(input_, api_key=api_key))


def update_issue(
    issue_id: str,
    update: IssueUpdateInput,
    *,
    api_key: str | None = None,
) -> Issue:
    return asyncio.run(update_issue_async(issue_id, update, api_key=api_key))
