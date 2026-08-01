#!/usr/bin/env python3
# ruff: noqa: N999
# trunk-ignore-all(bandit/B607): list-arg subprocess/argv only; tool names resolved via PATH.
"""Inspect and selectively resolve GitHub PR review threads via Dagger.

Replaces the ad hoc manual workflow of resolving a PR, paginating
`gh api graphql` for reviewThreads/reviews/comments, and hand-picking
unresolved CodeRabbit threads to resolve. Read-only `inspect` is the default;
`resolve` mutates only caller-selected thread IDs and never guesses.

By default this script runs `gh` inside a Dagger container. Set
`GTM_PR_REVIEW_VIA_FLOX=1` to instead run `gh` via a Flox-activated host
shell (`scripts/lib/flox.py::flox_activate_prefix()`) -- the fallback for
Conductor cloud sandboxes, where Dagger's container engine cannot start at
all (issue #284; do not reinvestigate). `gh` is already pinned in
`.flox/env/manifest.toml`, so no manifest change was needed for this script.
GH_TOKEN is passed to the Flox-run `gh` only via an explicit subprocess env
dict, never inherited raw from this process's own environment. See
AGENTS.md's "Dagger-fallback pattern (Flox)" section for the pattern shared
with `scripts/webhooks-handlers-redeploy.py` and
`scripts/hookdeck-connection_events-dump.py`.

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
import asyncio  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
from collections.abc import Callable  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Any  # noqa: E402

import dagger  # noqa: E402

from scripts.lib.env import env_flag  # noqa: E402
from scripts.lib.flox import flox_activate_prefix  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_API_ERROR = 3
EXIT_REFUSAL = 4

# Pinned to match scripts/webhooks-handlers-redeploy.py's DAGGER_BASE_IMAGE
# for toolchain consistency across the repo's Dagger pipelines.
DAGGER_BASE_IMAGE = "ghcr.io/astral-sh/uv:0.11.29-python3.13-trixie-slim"

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


# ---------------------------------------------------------------------------
# Dagger transport -- the only impure boundary besides `resolve_current_pr`.
# ---------------------------------------------------------------------------


def _content_addressed_secret_name(base: str, value: str) -> str:
    """Derive a Dagger secret name that changes when `value` changes.

    `dagger.dag.set_secret(name, value)` caches downstream `with_exec` steps
    keyed on the secret's name, not its plaintext -- reusing a fixed name
    across a rotated token would silently replay a stale cached `gh` call
    (including a stale mutation result) against the OLD credentials.
    Mirrors `scripts/webhooks-handlers-redeploy.py`'s helper of the same name.
    """
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"{base}-{digest}"


async def _run_gh_in_dagger(args: list[str], gh_token: str) -> str:
    async with dagger.connection(dagger.Config(log_output=sys.stderr)):
        token_secret = dagger.dag.set_secret(
            _content_addressed_secret_name("gh-token", gh_token),
            gh_token,
        )
        container = (
            dagger.dag.container()
            .from_(DAGGER_BASE_IMAGE)
            .with_exec(
                [
                    "sh",
                    "-c",
                    "apt-get update && apt-get install -y --no-install-recommends gh",
                ],
            )
            .with_secret_variable("GH_TOKEN", token_secret)
        )
        return await container.with_exec(["gh", *args]).stdout()


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


def make_dagger_run_gh(gh_token: str) -> RunGh:
    """Return a `RunGh` that always executes `gh` inside a Dagger container.

    Every call opens its own Dagger session -- acceptable for this CLI's
    invocation volume (one process, a handful of paginated calls), and it
    keeps the transport boundary a single, simple function to fake in tests.
    """

    def _run(args: list[str]) -> str:
        return asyncio.run(_run_gh_in_dagger(args, gh_token))

    return _run


# ---------------------------------------------------------------------------
# Flox transport -- the fallback for sandboxes with no Dagger engine.
# ---------------------------------------------------------------------------


def _run_gh_in_flox(args: list[str], gh_token: str) -> str:
    """Run `gh` via a Flox-activated host shell instead of a Dagger container.

    Unlike the Dagger path there is no `with_exec` cache to defeat, so
    `GH_TOKEN` doesn't need `_content_addressed_secret_name`'s hashing trick
    -- it's just passed through an explicit subprocess env dict (never the
    inherited raw environment) so a caller who happens to already export
    GH_TOKEN for something else can't accidentally supply the wrong token.
    """
    env = {**os.environ, "GH_TOKEN": gh_token}
    proc = subprocess.run(  # noqa: S603 — argv list, shell disabled
        [*flox_activate_prefix(REPO_ROOT), "gh", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def make_flox_run_gh(gh_token: str) -> RunGh:
    """Return a `RunGh` that executes `gh` via `flox activate` on the host.

    Parallel to `make_dagger_run_gh` -- selected instead of it when
    `GTM_PR_REVIEW_VIA_FLOX` is set (see `_use_flox`).
    """

    def _run(args: list[str]) -> str:
        return _run_gh_in_flox(args, gh_token)

    return _run


def _use_flox() -> bool:
    """Whether `GTM_PR_REVIEW_VIA_FLOX` selects the Flox transport.

    Routed through `env_flag` so `GTM_PR_REVIEW_VIA_FLOX=true` fails loudly
    instead of silently selecting Dagger, matching
    `scripts/webhooks-handlers-redeploy.py`'s `_use_flox`.
    """
    return env_flag("GTM_PR_REVIEW_VIA_FLOX")


def make_run_gh(gh_token: str) -> RunGh:
    """Select the Flox or Dagger transport based on `GTM_PR_REVIEW_VIA_FLOX`."""
    if _use_flox():
        return make_flox_run_gh(gh_token)
    return make_dagger_run_gh(gh_token)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/pr-review-threads.py",
        description=(
            "Inspect and selectively resolve GitHub PR review threads via a "
            "Dagger-run `gh`. `inspect` is read-only; `resolve` mutates only "
            "explicitly selected --thread IDs. Runs `gh` via Dagger by "
            "default; set GTM_PR_REVIEW_VIA_FLOX=1 to run via Flox instead "
            "(Conductor cloud sandboxes cannot run Dagger -- see module "
            "docstring)."
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
