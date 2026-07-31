"""Tests for scripts/pr-review-threads.py.

Two layers, mirroring the split used elsewhere in this repo:

- Pure-logic tests (pagination merging, provider filtering, mutation safety)
  call the module's functions directly with a fake ``run_gh`` -- no
  subprocess, no Dagger.
- A mocked-Dagger test (mirroring tests/scripts/test_deploy_webhook_dagger.py)
  patches the `dagger` module the script imported and asserts the container
  chain: base image, `gh` install, GH_TOKEN injected as a secret (never plain
  env or argv), and the final `gh` exec.

Neither layer needs a live Dagger engine or real GitHub credentials.
"""

# ruff: noqa: S101, SLF001, TRY003, EM101 -- asserts are the point of a test file;
# SLF001 covers deliberate white-box use of the script's private helpers
# (_build_parser, etc); TRY003/EM101 cover inline test-fixture error messages.

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "pr-review-threads.py"
_MODULE_NAME = "_pr_review_threads_under_test"


@pytest.fixture(scope="module")
def prt() -> Iterator[ModuleType]:
    """Load scripts/pr-review-threads.py as a module without packaging it.

    `scripts/` is excluded from `[tool.setuptools.packages.find]`, so a
    normal import doesn't resolve -- load via file path instead.
    """
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(_MODULE_NAME, None)


def _threads_payload(
    nodes: list[dict[str, Any]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                        "nodes": nodes,
                    },
                },
            },
        },
    }


def _thread_node(
    thread_id: str,
    *,
    resolved: bool = False,
    outdated: bool = False,
    author: str = "coderabbitai[bot]",
    comment_has_next: bool = False,
) -> dict[str, Any]:
    return {
        "id": thread_id,
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": "a.py",
        "line": 3,
        "comments": {
            "pageInfo": {
                "hasNextPage": comment_has_next,
                "endCursor": "CUR" if comment_has_next else None,
            },
            "nodes": [
                {
                    "id": f"{thread_id}-c1",
                    "databaseId": 1,
                    "body": "please fix",
                    "author": {"login": author},
                    "createdAt": "2026-01-01T00:00:00Z",
                    "url": "https://example.com",
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# Pure logic: pagination merging
# ---------------------------------------------------------------------------


def test_fetch_review_threads_merges_across_pages(prt: ModuleType) -> None:
    page1 = _threads_payload([_thread_node("T1")], has_next=True, cursor="CUR1")
    page2 = _threads_payload([_thread_node("T2", resolved=True)])
    calls: list[list[str]] = []

    def fake_run_gh(args: list[str]) -> str:
        calls.append(args)
        return json.dumps(page1 if len(calls) == 1 else page2)

    threads = prt.fetch_review_threads("owner", "repo", 1, fake_run_gh)
    assert [t.id for t in threads] == ["T1", "T2"]
    assert threads[1].is_resolved is True


def test_fetch_review_threads_recursively_paginates_thread_comments(
    prt: ModuleType,
) -> None:
    first_page = _threads_payload([_thread_node("T1", comment_has_next=True)])
    thread_comments_page = {
        "data": {
            "node": {
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "T1-c2",
                            "databaseId": 2,
                            "body": "second page comment",
                            "author": {"login": "someone"},
                            "createdAt": "2026-01-02T00:00:00Z",
                            "url": None,
                        },
                    ],
                },
            },
        },
    }
    calls: list[list[str]] = []

    def fake_run_gh(args: list[str]) -> str:
        calls.append(args)
        if len(calls) == 1:
            return json.dumps(first_page)
        return json.dumps(thread_comments_page)

    threads = prt.fetch_review_threads("owner", "repo", 1, fake_run_gh)
    assert len(threads) == 1
    comment_ids = [c.id for c in threads[0].comments]
    assert comment_ids == ["T1-c1", "T1-c2"]


def test_graphql_errors_payload_raises(prt: ModuleType) -> None:
    def fake_run_gh(_args: list[str]) -> str:
        return json.dumps({"errors": [{"message": "Could not resolve to a node"}]})

    with pytest.raises(prt.GhApiError, match="GraphQL errors"):
        prt.fetch_review_threads("owner", "repo", 1, fake_run_gh)


def test_non_json_response_raises(prt: ModuleType) -> None:
    def fake_run_gh(_args: list[str]) -> str:
        return "not json"

    with pytest.raises(prt.GhApiError, match="non-JSON"):
        prt.fetch_review_threads("owner", "repo", 1, fake_run_gh)


# ---------------------------------------------------------------------------
# Pure logic: filtering
# ---------------------------------------------------------------------------


def test_filter_threads_by_provider_shorthand(prt: ModuleType) -> None:
    threads = [
        prt.Thread(
            id="T1",
            is_resolved=False,
            is_outdated=False,
            path="a.py",
            line=1,
            comments=[
                prt.Comment(
                    id="c1",
                    database_id=1,
                    body="x",
                    author_login="coderabbitai[bot]",
                    created_at="t",
                ),
            ],
        ),
        prt.Thread(
            id="T2",
            is_resolved=False,
            is_outdated=False,
            path="a.py",
            line=2,
            comments=[
                prt.Comment(
                    id="c2",
                    database_id=2,
                    body="x",
                    author_login="human",
                    created_at="t",
                ),
            ],
        ),
    ]
    filtered = prt.filter_threads(threads, provider="coderabbitai")
    assert [t.id for t in filtered] == ["T1"]


def test_filter_threads_unresolved_only(prt: ModuleType) -> None:
    threads = [
        prt.Thread(id="T1", is_resolved=True, is_outdated=False, path="a.py", line=1),
        prt.Thread(id="T2", is_resolved=False, is_outdated=False, path="a.py", line=2),
    ]
    filtered = prt.filter_threads(threads, unresolved_only=True)
    assert [t.id for t in filtered] == ["T2"]


# ---------------------------------------------------------------------------
# Pure logic: mutation safety (plan_mutations)
# ---------------------------------------------------------------------------


def _threads_fixture(prt: ModuleType) -> list[Any]:
    return [
        prt.Thread(
            id="RESOLVED",
            is_resolved=True,
            is_outdated=False,
            path="a.py",
            line=1,
        ),
        prt.Thread(
            id="OUTDATED",
            is_resolved=False,
            is_outdated=True,
            path="a.py",
            line=2,
        ),
        prt.Thread(
            id="OPEN",
            is_resolved=False,
            is_outdated=False,
            path="a.py",
            line=3,
        ),
    ]


def test_plan_mutations_unknown_id_refuses(prt: ModuleType) -> None:
    plan = prt.plan_mutations(_threads_fixture(prt), ["NOPE"], allow_outdated=False)
    assert isinstance(plan, prt.MutationRefusal)
    assert "NOPE" in plan.reason


def test_plan_mutations_outdated_without_override_refuses(prt: ModuleType) -> None:
    plan = prt.plan_mutations(_threads_fixture(prt), ["OUTDATED"], allow_outdated=False)
    assert isinstance(plan, prt.MutationRefusal)
    assert "OUTDATED" in plan.reason


def test_plan_mutations_outdated_with_override_proceeds(prt: ModuleType) -> None:
    plan = prt.plan_mutations(_threads_fixture(prt), ["OUTDATED"], allow_outdated=True)
    assert isinstance(plan, prt.MutationPlan)
    assert plan.to_mutate == ["OUTDATED"]


def test_plan_mutations_already_resolved_is_noop(prt: ModuleType) -> None:
    plan = prt.plan_mutations(_threads_fixture(prt), ["RESOLVED"], allow_outdated=False)
    assert isinstance(plan, prt.MutationPlan)
    assert plan.to_mutate == []
    assert plan.already_resolved == ["RESOLVED"]


def test_plan_mutations_mixed_open_and_resolved(prt: ModuleType) -> None:
    plan = prt.plan_mutations(
        _threads_fixture(prt),
        ["RESOLVED", "OPEN"],
        allow_outdated=False,
    )
    assert isinstance(plan, prt.MutationPlan)
    assert plan.to_mutate == ["OPEN"]
    assert plan.already_resolved == ["RESOLVED"]


def test_plan_mutations_unknown_id_blocks_even_with_valid_ids(prt: ModuleType) -> None:
    """All-or-nothing: one bad ID refuses the whole batch, valid IDs included."""
    plan = prt.plan_mutations(
        _threads_fixture(prt),
        ["OPEN", "NOPE"],
        allow_outdated=False,
    )
    assert isinstance(plan, prt.MutationRefusal)


# ---------------------------------------------------------------------------
# CLI-level behavior (inspect never mutates, resolve wiring), via injected run_gh
# ---------------------------------------------------------------------------


def test_cmd_inspect_never_calls_resolve_mutation(prt: ModuleType) -> None:
    calls: list[list[str]] = []

    def fake_run_gh(args: list[str]) -> str:
        calls.append(args)
        return json.dumps(_threads_payload([_thread_node("T1")]))

    args = prt._build_parser().parse_args(
        ["inspect", "--repo", "o/r", "--pr", "1", "--format", "json"],
    )
    exit_code = prt._cmd_inspect(args, fake_run_gh)
    assert exit_code == prt.EXIT_OK
    assert not any("resolveReviewThread" in "".join(c) for c in calls)


def test_cmd_resolve_mutates_only_selected_thread(prt: ModuleType) -> None:
    mutation_calls: list[list[str]] = []

    def fake_run_gh(args: list[str]) -> str:
        joined = " ".join(args)
        if "resolveReviewThread" in joined:
            mutation_calls.append(args)
            return json.dumps(
                {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {
                                "id": "T1",
                                "isResolved": True,
                                "isOutdated": False,
                            },
                        },
                    },
                },
            )
        return json.dumps(
            _threads_payload([_thread_node("T1"), _thread_node("T2", resolved=True)]),
        )

    args = prt._build_parser().parse_args(
        ["resolve", "--repo", "o/r", "--pr", "1", "--thread", "T1", "--thread", "T2"],
    )
    exit_code = prt._cmd_resolve(args, fake_run_gh)
    assert exit_code == prt.EXIT_OK
    # Only T1 (open) triggers a mutation call; T2 is already resolved (no-op).
    assert len(mutation_calls) == 1


def test_cmd_resolve_refuses_unknown_thread(prt: ModuleType) -> None:
    def fake_run_gh(_args: list[str]) -> str:
        return json.dumps(_threads_payload([_thread_node("T1")]))

    args = prt._build_parser().parse_args(
        ["resolve", "--repo", "o/r", "--pr", "1", "--thread", "NOPE"],
    )
    exit_code = prt._cmd_resolve(args, fake_run_gh)
    assert exit_code == prt.EXIT_REFUSAL


def test_cmd_resolve_reports_partial_progress_on_mutation_failure(
    prt: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A later mutation failing must not hide the earlier ones that succeeded."""
    mutated_ids: list[str] = []

    def fake_run_gh(args: list[str]) -> str:
        joined = " ".join(args)
        if "resolveReviewThread" in joined:
            if "T2" in joined:
                raise prt.GhApiError("simulated network failure")
            mutated_ids.append("T1")
            return json.dumps(
                {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {
                                "id": "T1",
                                "isResolved": True,
                                "isOutdated": False,
                            },
                        },
                    },
                },
            )
        return json.dumps(_threads_payload([_thread_node("T1"), _thread_node("T2")]))

    args = prt._build_parser().parse_args(
        [
            "resolve",
            "--repo",
            "o/r",
            "--pr",
            "1",
            "--thread",
            "T1",
            "--thread",
            "T2",
            "--format",
            "json",
        ],
    )
    exit_code = prt._cmd_resolve(args, fake_run_gh)
    assert exit_code == prt.EXIT_API_ERROR
    assert mutated_ids == ["T1"]

    captured = capsys.readouterr()
    reported = json.loads(captured.out)
    assert reported == [
        {
            "thread_id": "T1",
            "previously_resolved": False,
            "now_resolved": True,
            "outdated": False,
        },
    ]
    assert "simulated network failure" in captured.err


# ---------------------------------------------------------------------------
# PR/repo resolution: fork-safe base-repo parsing
# ---------------------------------------------------------------------------


def test_resolve_current_pr_uses_base_repo_from_url(prt: ModuleType) -> None:
    """For a fork-based PR, headRepository is the fork -- url names the base repo."""

    def fake_run_gh(_args: list[str]) -> str:
        return json.dumps(
            {
                "number": 42,
                "url": "https://github.com/upstream-owner/upstream-repo/pull/42",
            },
        )

    owner, repo, number = prt.resolve_current_pr(fake_run_gh)
    assert (owner, repo, number) == ("upstream-owner", "upstream-repo", 42)


# ---------------------------------------------------------------------------
# Mocked-Dagger transport: container chain, secret injection, no token leakage
# ---------------------------------------------------------------------------


def _build_dagger_mock(stdout_value: str) -> tuple[MagicMock, MagicMock]:
    """Return ``(fake_dagger_module, final_exec_container)``.

    The script's real chain is fixed and shallow (from_ -> with_exec ->
    with_secret_variable -> with_exec), so unlike the webhook-deploy test's
    generic chain tracker, a straight-line mock is sufficient here.
    """
    final_container = MagicMock(name="final_container")
    final_container.stdout = AsyncMock(return_value=stdout_value)

    secret_container = MagicMock(name="secret_container")
    secret_container.with_exec.return_value = final_container

    installed_container = MagicMock(name="installed_container")
    installed_container.with_secret_variable.return_value = secret_container

    base_container = MagicMock(name="base_container")
    base_container.with_exec.return_value = installed_container

    dag = MagicMock(name="dag")
    dag.container.return_value.from_.return_value = base_container
    def set_secret(name: str, value: str) -> MagicMock:
        return MagicMock(_secret=(name, value))

    dag.set_secret.side_effect = set_secret

    connection_cm = MagicMock(name="connection_cm")
    connection_cm.__aenter__ = AsyncMock(return_value=None)
    connection_cm.__aexit__ = AsyncMock(return_value=None)

    fake_dagger = MagicMock(name="dagger_module")
    fake_dagger.connection.return_value = connection_cm
    fake_dagger.Config = MagicMock(name="Config")
    fake_dagger.dag = dag

    return fake_dagger, final_container


def test_dagger_transport_injects_token_as_secret_not_env(prt: ModuleType) -> None:
    fake_dagger, final_container = _build_dagger_mock(stdout_value='{"data": {}}')

    with patch.object(prt, "dagger", fake_dagger):
        result = prt.asyncio.run(prt._run_gh_in_dagger(["pr", "view"], "sekret-token"))

    assert result == '{"data": {}}'
    fake_dagger.dag.container.return_value.from_.assert_called_once_with(
        prt.DAGGER_BASE_IMAGE,
    )
    expected_name = prt._content_addressed_secret_name("gh-token", "sekret-token")
    fake_dagger.dag.set_secret.assert_called_once_with(expected_name, "sekret-token")

    secret_container = (
        fake_dagger.dag.container.return_value.from_.return_value.with_exec.return_value
    )
    # Secret injected via with_secret_variable("GH_TOKEN", <secret>), never as
    # a plain with_env_variable or baked into the with_exec argv.
    name, secret_obj = secret_container.with_secret_variable.call_args[0]
    assert name == "GH_TOKEN"
    assert secret_obj._secret == (expected_name, "sekret-token")

    final_exec_args = (
        secret_container.with_secret_variable.return_value.with_exec.call_args[0][0]
    )
    assert final_exec_args == ["gh", "pr", "view"]
    assert "sekret-token" not in json.dumps(final_exec_args)
    final_container.stdout.assert_awaited_once()
