#!/usr/bin/env python3
# ruff: noqa: N999
# trunk-ignore-all(bandit/B607): list-arg subprocess/argv only; tool names resolved via PATH.
"""Inspect and selectively resolve GitHub PR review threads via Dagger.

Replaces the ad hoc manual workflow of resolving a PR, paginating
`gh api graphql` for reviewThreads/reviews/comments, and hand-picking
unresolved CodeRabbit threads to resolve. Read-only `inspect` is the default;
`resolve` mutates only caller-selected thread IDs and never guesses.

By default this script runs `gh` in the repo's activated Flox environment.
Set `RUN_WITH_DAGGER=1` to opt into the shared Dagger wrapper, which uses the
same prebuilt Flox toolchain image. GH_TOKEN is passed only as an explicit
environment value or Dagger secret.

Usage:
    scripts/pr-review-threads.py inspect [--repo OWNER/REPO] [--pr NUMBER]
        [--provider LOGIN] [--unresolved-only] [--format table|json]
    scripts/pr-review-threads.py resolve --repo OWNER/REPO --pr NUMBER
        --thread THREAD_ID [--thread THREAD_ID ...] [--allow-outdated]
        [--format table|json]

Exit codes: 0 success, 2 usage error (argparse native), 3 auth/API/GraphQL
failure, 4 refusal (unsafe/unknown/outdated mutation request).

Safety model:
  - `inspect` never invokes the resolveReviewThread mutation.
  - `resolve` requires explicit `--thread` IDs. Any unknown ID, or any
    outdated thread without `--allow-outdated`, refuses the whole request
    before any mutation runs. GitHub has no batch resolve mutation, so this
    guarantee covers validation, not the mutation calls themselves -- if a
    later mutation fails after earlier ones already succeeded, the command
    reports what it actually resolved and exits non-zero rather than
    silently losing that partial progress.
  - Already-resolved thread IDs are reported as no-ops; no mutation call is
    issued for them.
  - GH_TOKEN is injected into the Dagger container only as a secret env var
    -- never in argv, image layers, logs, or this script's own output.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts.lib.uv_bootstrap import bootstrap_uv as _bootstrap_uv  # noqa: E402

if __name__ == "__main__":
    _bootstrap_uv(script_path=__file__, mode="python")

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
from collections.abc import Callable  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Any  # noqa: E402

from scripts.lib.container import (  # noqa: E402
    RUN_WITH_DAGGER,
    in_container_phase,
    run_in_container,
)
from scripts.lib.env import env_flag  # noqa: E402
from scripts.lib.flox import run as flox_run  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_API_ERROR = 3
EXIT_REFUSAL = 4

CODERABBIT_LOGIN = "coderabbitai[bot]"
_PROVIDER_ALIASES: dict[str, str] = {"coderabbitai": CODERABBIT_LOGIN}

_PAGE_SIZE = 50

_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id
      reviewThreads(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            pageInfo { hasNextPage endCursor }
            nodes { id databaseId body author { login } createdAt url }
          }
        }
      }
    }
  }
}
"""

_THREAD_COMMENTS_QUERY = """
query($threadId: ID!, $after: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId body author { login } createdAt url }
      }
    }
  }
}
"""

_REVIEWS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviews(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId state author { login } submittedAt body }
      }
    }
  }
}
"""

_COMMENTS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      comments(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId author { login } body createdAt url }
      }
    }
  }
}
"""

_RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: { threadId: $threadId }) {
    thread { id isResolved isOutdated }
  }
}
"""

RunGh = Callable[[list[str]], str]


class GhApiError(RuntimeError):
    """Raised when `gh` fails or returns a GraphQL error payload."""


@dataclass
class Comment:
    id: str
    database_id: int | None
    body: str
    author_login: str | None
    created_at: str
    url: str | None = None


@dataclass
class Thread:
    id: str
    is_resolved: bool
    is_outdated: bool
    path: str
    line: int | None
    comments: list[Comment] = field(default_factory=list)

    @property
    def author_logins(self) -> set[str]:
        return {c.author_login for c in self.comments if c.author_login}


@dataclass
class ReviewState:
    threads: list[Thread]
    reviews: list[dict[str, Any]]
    issue_comments: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Pure GraphQL parsing / pagination (no subprocess calls in this section)
# ---------------------------------------------------------------------------


def _run_graphql(
    run_gh: RunGh,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    """Invoke `gh api graphql` with the given query/variables and parse JSON.

    Raises GhApiError on a non-JSON response or a GraphQL `errors` payload --
    `gh api graphql` exits non-zero for HTTP failures but can also return
    exit 0 with an `errors` array for query-level failures (e.g. a bad node
    ID), so both cases must be checked explicitly.
    """
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        args.extend(["-F", f"{key}={value}"])
    try:
        raw = run_gh(args)
    except GhApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - transport is pluggable (host subprocess or Dagger)
        stderr = (getattr(exc, "stderr", "") or "").strip()
        msg = f"gh api graphql failed: {stderr or exc}"
        raise GhApiError(msg) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"gh api graphql returned non-JSON output: {raw[:500]!r}"
        raise GhApiError(msg) from exc
    if payload.get("errors"):
        msg = f"GraphQL errors: {payload['errors']}"
        raise GhApiError(msg)
    return payload


def _paginate_thread_comments(run_gh: RunGh, thread: Thread, after: str | None) -> None:
    """Fully paginate a single thread's nested `comments` connection in place.

    `after` is the first page's own `endCursor` (already fetched by the main
    threads query) -- starting from `None` here would refetch page 1 as
    page 2, silently duplicating comments instead of advancing.
    """
    while True:
        payload = _run_graphql(
            run_gh,
            _THREAD_COMMENTS_QUERY,
            {"threadId": thread.id, "after": after},
        )
        connection = payload["data"]["node"]["comments"]
        for node in connection["nodes"]:
            comment_id = node["id"]
            if any(c.id == comment_id for c in thread.comments):
                continue
            thread.comments.append(_comment_from_node(node))
        if not connection["pageInfo"]["hasNextPage"]:
            return
        after = connection["pageInfo"]["endCursor"]


def _comment_from_node(node: dict[str, Any]) -> Comment:
    author = node.get("author") or {}
    return Comment(
        id=node["id"],
        database_id=node.get("databaseId"),
        body=node.get("body", ""),
        author_login=author.get("login"),
        created_at=node.get("createdAt", ""),
        url=node.get("url"),
    )


def _thread_from_node(node: dict[str, Any]) -> tuple[Thread, bool, str | None]:
    """Return the parsed thread, whether its first comment page had more, and its cursor."""
    thread = Thread(
        id=node["id"],
        is_resolved=node["isResolved"],
        is_outdated=node["isOutdated"],
        path=node["path"],
        line=node.get("line"),
        comments=[_comment_from_node(n) for n in node["comments"]["nodes"]],
    )
    page_info = node["comments"]["pageInfo"]
    return thread, page_info["hasNextPage"], page_info["endCursor"]


def fetch_review_threads(
    owner: str,
    repo: str,
    number: int,
    run_gh: RunGh,
) -> list[Thread]:
    """Fully paginate `reviewThreads`, including each thread's own comments."""
    threads: list[Thread] = []
    needs_more_comments: list[tuple[Thread, str | None]] = []
    after = None
    while True:
        payload = _run_graphql(
            run_gh,
            _THREADS_QUERY,
            {"owner": owner, "repo": repo, "number": number, "after": after},
        )
        connection = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
        for node in connection["nodes"]:
            thread, had_more_comments, comment_cursor = _thread_from_node(node)
            threads.append(thread)
            if had_more_comments:
                needs_more_comments.append((thread, comment_cursor))
        if not connection["pageInfo"]["hasNextPage"]:
            break
        after = connection["pageInfo"]["endCursor"]

    for thread, comment_cursor in needs_more_comments:
        _paginate_thread_comments(run_gh, thread, comment_cursor)
    return threads


def fetch_reviews(
    owner: str,
    repo: str,
    number: int,
    run_gh: RunGh,
) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    after = None
    while True:
        payload = _run_graphql(
            run_gh,
            _REVIEWS_QUERY,
            {"owner": owner, "repo": repo, "number": number, "after": after},
        )
        connection = payload["data"]["repository"]["pullRequest"]["reviews"]
        reviews.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            return reviews
        after = connection["pageInfo"]["endCursor"]


def fetch_issue_comments(
    owner: str,
    repo: str,
    number: int,
    run_gh: RunGh,
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    after = None
    while True:
        payload = _run_graphql(
            run_gh,
            _COMMENTS_QUERY,
            {"owner": owner, "repo": repo, "number": number, "after": after},
        )
        connection = payload["data"]["repository"]["pullRequest"]["comments"]
        comments.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            return comments
        after = connection["pageInfo"]["endCursor"]


def fetch_review_state(
    owner: str,
    repo: str,
    number: int,
    run_gh: RunGh,
) -> ReviewState:
    return ReviewState(
        threads=fetch_review_threads(owner, repo, number, run_gh),
        reviews=fetch_reviews(owner, repo, number, run_gh),
        issue_comments=fetch_issue_comments(owner, repo, number, run_gh),
    )


def normalize_provider(provider: str) -> str:
    return _PROVIDER_ALIASES.get(provider, provider)


def filter_threads(
    threads: list[Thread],
    *,
    provider: str | None = None,
    unresolved_only: bool = False,
) -> list[Thread]:
    result = threads
    if provider:
        login = normalize_provider(provider)
        result = [t for t in result if login in t.author_logins]
    if unresolved_only:
        result = [t for t in result if not t.is_resolved]
    return result


# ---------------------------------------------------------------------------
# Mutation safety logic (pure given a resolved thread list)
# ---------------------------------------------------------------------------


@dataclass
class MutationRefusal:
    reason: str


@dataclass
class MutationPlan:
    to_mutate: list[str]
    already_resolved: list[str]


def plan_mutations(
    threads: list[Thread],
    requested_ids: list[str],
    *,
    allow_outdated: bool,
) -> MutationPlan | MutationRefusal:
    """Validate `requested_ids` against the fetched thread set.

    Verifying threads "belong to the requested PR" is exactly this lookup --
    `threads` was fetched from precisely (owner, repo, pr_number), so any ID
    not present in `by_id` is either unknown or from a different PR. No
    separate verification round-trip is needed.
    """
    by_id = {t.id: t for t in threads}
    unknown = [tid for tid in requested_ids if tid not in by_id]
    if unknown:
        return MutationRefusal(f"thread(s) not found on this PR: {', '.join(unknown)}")

    if not allow_outdated:
        blocked = [tid for tid in requested_ids if by_id[tid].is_outdated]
        if blocked:
            return MutationRefusal(
                f"outdated thread(s) require --allow-outdated: {', '.join(blocked)}",
            )

    already_resolved = [tid for tid in requested_ids if by_id[tid].is_resolved]
    to_mutate = [tid for tid in requested_ids if tid not in already_resolved]
    return MutationPlan(to_mutate=to_mutate, already_resolved=already_resolved)


def resolve_thread(thread_id: str, run_gh: RunGh) -> dict[str, Any]:
    payload = _run_graphql(run_gh, _RESOLVE_MUTATION, {"threadId": thread_id})
    return payload["data"]["resolveReviewThread"]["thread"]


# ---------------------------------------------------------------------------
# PR/repo resolution
# ---------------------------------------------------------------------------


def resolve_current_pr(run_gh: RunGh) -> tuple[str, str, int]:
    """Resolve (owner, repo, number) for the current branch's PR.

    Review threads live on the PR's *base* repository, not its head. For a
    fork-based PR those differ -- `headRepositoryOwner`/`headRepository`
    would point at the contributor's fork, and querying reviewThreads there
    with the upstream PR number returns nothing. `url` (e.g.
    https://github.com/OWNER/REPO/pull/NUMBER) always names the base repo.
    """
    raw = run_gh(["pr", "view", "--json", "number,url"])
    data = json.loads(raw)
    match = re.match(r"^https://github\.com/([^/]+)/([^/]+)/pull/\d+$", data["url"])
    if match is None:
        msg = f"Could not parse owner/repo from PR url: {data['url']!r}"
        raise GhApiError(msg)
    owner, repo = match.group(1), match.group(2)
    return owner, repo, data["number"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_threads(threads: list[Thread], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(
            [
                {
                    "id": t.id,
                    "is_resolved": t.is_resolved,
                    "is_outdated": t.is_outdated,
                    "path": t.path,
                    "line": t.line,
                    "authors": sorted(t.author_logins),
                    "comments": [
                        {
                            "id": c.id,
                            "author": c.author_login,
                            "body": c.body,
                            "created_at": c.created_at,
                        }
                        for c in t.comments
                    ],
                }
                for t in threads
            ],
            indent=2,
        )
    lines = [
        f"{'RESOLVED' if t.is_resolved else 'OPEN':8} "
        f"{'OUTDATED' if t.is_outdated else '':8} "
        f"{t.path}:{t.line}  [{t.id}]  {sorted(t.author_logins)}"
        for t in threads
    ]
    return "\n".join(lines) if lines else "(no matching threads)"


def _resolve_gh_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    proc = subprocess.run(
        ["gh", "auth", "token"],  # noqa: S607 - `gh` resolved via PATH on purpose
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        msg = "No GH_TOKEN/GITHUB_TOKEN set and `gh auth token` failed. Authenticate gh first."
        raise GhApiError(msg)
    return proc.stdout.strip()


def make_run_gh(gh_token: str) -> RunGh:
    """Run ``gh`` in Flox, or re-exec this script through the container wrapper."""

    def _run(args: list[str]) -> str:
        if env_flag(RUN_WITH_DAGGER) and not in_container_phase():
            return (
                run_in_container(
                    repo_root=REPO_ROOT,
                    argv=["gh", *args],
                    secrets={"GH_TOKEN": gh_token},
                    capture=True,
                )
                or ""
            )
        return (
            flox_run(
                ["gh", *args],
                repo_root=REPO_ROOT,
                env={"GH_TOKEN": gh_token},
                capture=True,
            )
            or ""
        )

    return _run


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/pr-review-threads.py",
        description=(
            "Inspect and selectively resolve GitHub PR review threads via a "
            "Flox-run `gh`. `inspect` is read-only; `resolve` mutates only "
            "explicitly selected --thread IDs. Set RUN_WITH_DAGGER=1 to "
            "opt into the shared container wrapper."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="read-only: list review threads")
    inspect.add_argument(
        "--repo",
        help="OWNER/REPO; defaults to the current branch's PR",
    )
    inspect.add_argument(
        "--pr",
        type=int,
        help="PR number; defaults to current branch's PR",
    )
    inspect.add_argument(
        "--provider",
        help="filter by comment author login, e.g. coderabbitai",
    )
    inspect.add_argument("--unresolved-only", action="store_true")
    inspect.add_argument("--format", choices=["table", "json"], default="table")

    resolve = sub.add_parser(
        "resolve",
        help="mutate: resolve explicitly selected threads",
    )
    resolve.add_argument("--repo", required=True)
    resolve.add_argument("--pr", required=True, type=int)
    resolve.add_argument("--thread", action="append", dest="threads", required=True)
    resolve.add_argument("--allow-outdated", action="store_true")
    resolve.add_argument("--format", choices=["table", "json"], default="table")

    return parser


def _resolve_repo_and_pr(
    args: argparse.Namespace,
    run_gh: RunGh,
) -> tuple[str, str, int]:
    if args.repo and args.pr:
        owner, repo = args.repo.split("/", 1)
        return owner, repo, args.pr
    return resolve_current_pr(run_gh)


def _cmd_inspect(args: argparse.Namespace, run_gh: RunGh) -> int:
    owner, repo, number = _resolve_repo_and_pr(args, run_gh)
    threads = fetch_review_threads(owner, repo, number, run_gh)
    filtered = filter_threads(
        threads,
        provider=args.provider,
        unresolved_only=args.unresolved_only,
    )
    print(render_threads(filtered, args.format))
    return EXIT_OK


def _cmd_resolve(args: argparse.Namespace, run_gh: RunGh) -> int:
    """Resolve the caller's selected threads.

    "All-or-nothing" applies to *validation*: an unknown ID or an outdated
    thread without --allow-outdated refuses the whole request before any
    mutation call is made (see plan_mutations). It does not make the actual
    GitHub mutations atomic -- GitHub has no batch resolveReviewThread call,
    so if a later mutation fails after earlier ones already succeeded, this
    reports the successfully-mutated threads and returns EXIT_API_ERROR
    rather than silently losing that partial progress.
    """
    owner, repo, number = _resolve_repo_and_pr(args, run_gh)
    threads = fetch_review_threads(owner, repo, number, run_gh)
    plan = plan_mutations(threads, args.threads, allow_outdated=args.allow_outdated)
    if isinstance(plan, MutationRefusal):
        print(f"REFUSED: {plan.reason}", file=sys.stderr)
        return EXIT_REFUSAL

    results = []
    for tid in plan.already_resolved:
        results.append(
            {"thread_id": tid, "previously_resolved": True, "now_resolved": True},
        )
    exit_code = EXIT_OK
    for tid in plan.to_mutate:
        try:
            mutated = resolve_thread(tid, run_gh)
        except GhApiError as exc:
            print(f"ERROR: failed to resolve {tid}: {exc}", file=sys.stderr)
            exit_code = EXIT_API_ERROR
            break
        results.append(
            {
                "thread_id": tid,
                "previously_resolved": False,
                "now_resolved": mutated["isResolved"],
                "outdated": mutated["isOutdated"],
            },
        )

    if args.format == "json":
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"{r['thread_id']}  now_resolved={r['now_resolved']}")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        gh_token = _resolve_gh_token()
    except GhApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_API_ERROR
    run_gh = make_run_gh(gh_token)

    try:
        if args.command == "inspect":
            return _cmd_inspect(args, run_gh)
        return _cmd_resolve(args, run_gh)
    except GhApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_API_ERROR


if __name__ == "__main__":
    sys.exit(main())
