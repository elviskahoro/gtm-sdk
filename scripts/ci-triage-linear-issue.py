#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["gtm-linear==0.1.0"]
# ///
r"""File (or bump) a Linear issue describing a failed CI run.

Reads a diagnosis written by the triage agent and turns it into exactly one
Linear issue per distinct failing workflow. A recurring failure updates the
existing issue's metadata footer instead of filing a duplicate -- the nightly
Integration tests were red 13 nights running (2026-07-15..2026-07-27), and that
is one issue, not thirteen.

How this stays runnable while CI is broken:

This runs in the CI job that reacts to a broken build, so it must not depend on
the repo's environment resolving -- ``uv sync`` is itself a plausible cause of
the failure being triaged. It gets its one dependency without ever reading
``uv.lock``: the PEP 723 header above pins ``gtm-linear`` for the host path
(``uv run --script`` resolves it into an ephemeral env, isolated from the
project), and the Dagger container installs the same pin as its own ``pip``
layer. Neither route resolves this repo's dependency graph, so a broken
lockfile or a poisoned working tree still cannot stop a ticket being filed.
What it does now depend on is PyPI being reachable -- a narrower exposure than
hand-rolled GraphQL was worth.

``uv run --script`` picks up the sibling ``ci-triage-linear-issue.py.lock``
script lockfile automatically, so the host path's full transitive closure is
hash-verified too, not just the top-level pin. Regenerate it with
``uv lock --script scripts/ci-triage-linear-issue.py`` whenever the PEP 723
``dependencies`` line changes.

Do not reintroduce a raw-GraphQL fallback path. Every Linear call goes through
``libs.linear.client``; that adapter is where the API surface belongs, and a
second implementation that only runs when the first is missing is a code path
CI can never tell you it took.

In CI this runs inside a Dagger container -- see
``.github/workflows/ci/triage_dagger.py``, which is what
``.github/workflows/agent-ci-triage.yml`` invokes. It is still directly runnable
on a host, which is the workflow's fallback path when the Dagger engine is
unavailable:

    LINEAR_API_KEY=lin_api_xxx scripts/ci-triage-linear-issue.py \\
        --workflow "Unit tests" \\
        --run-url https://github.com/o/r/actions/runs/123 \\
        --branch main --commit abc1234 \\
        --diagnosis-file tmp/diagnosis.md

That path needs ``uv`` on PATH, which is why the workflow installs it before
the fallback step runs.

The target team is the hard-coded ``LINEAR_TEAM`` below; ``--team`` overrides it
and accepts a key or a UUID.

Exits non-zero only on misconfiguration or an API error. A missing or empty
diagnosis file is reported and exits 0, so a flaky agent step cannot turn one
red check into two.
"""

from __future__ import annotations

import pathlib as _pathlib
import sys as _sys

_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))

# The bootstrap re-execs this file through a `[tool.uv] required-version`-
# compatible `uv` so the PEP 723 header above is honoured. It is optional, not
# required: the Dagger container mounts this script and `libs/linear/` and
# nothing else, so `scripts/lib/` is absent there and its dependency is already
# installed. Importing it unconditionally made the containerized filing path die
# with ModuleNotFoundError before main() ran, silently routing every ticket
# through the host fallback. OSError covers uv_resolve's import-time hunt for a
# pyproject.toml, which has none to find in a minimal tree.
try:
    from scripts.lib.uv_bootstrap import bootstrap_uv as _bootstrap_uv  # noqa: E402
except (ImportError, OSError):  # pragma: no cover - exercised by the container test
    _bootstrap_uv = None  # type: ignore[assignment]

if __name__ == "__main__" and _bootstrap_uv is not None:
    _bootstrap_uv(script_path=__file__, mode="script")

import argparse
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from gtm_linear import IssueCreateInput, IssueUpdateInput
from gtm_linear.exceptions import LinearError as _SdkLinearError
from pydantic import ValidationError

from libs.linear import client as linear

if TYPE_CHECKING:
    from collections.abc import Generator

    from gtm_linear import Issue

# Hard-coded on purpose: there is exactly one team that owns this repo's CI, so
# routing it through a GitHub secret bought nothing but another value to forget to
# set (and a preflight branch that silently skipped triage when it was missing).
# A team KEY rather than a UUID because it is legible in a diff -- ``resolve_team``
# converts it with one extra GraphQL round-trip, since Linear's ``issueCreate``
# only accepts the UUID form. Override with --team for local experiments.
LINEAR_TEAM = "AI"

# Sentinel that makes an issue findable on the next failure of the same
# workflow. Lives in the description because Linear has no custom-field API on
# the free tier and `issueCreate` accepts only title/teamId/description.
MARKER_TEMPLATE = "<!-- ci-triage-key: {workflow} -->"

# Everything below this line in the description is script-managed. The agent's
# diagnosis lives above it and is never rewritten, so a human can edit it.
FOOTER_SENTINEL = "<!-- ci-triage-footer -->"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
# `[-*]`, not `-`: Linear rewrites list markers to `*` when it stores a
# description, so reading a footer back never sees the `-` build_footer wrote.
# Anchoring on `-` made both lookups miss on every bump -- occurrences pinned
# at the literal fallback of 2 (AI-1 and AI-2 sat there while the nightly suite
# failed far more often than twice) and "First seen" silently rewritten to the
# current run, which is the one field a human reads to judge how long something
# has been broken.
_OCCURRENCES_RE = re.compile(r"^[-*] Occurrences:\s*(\d+)", re.MULTILINE)
_FIRST_SEEN_RE = re.compile(r"^[-*] First seen:\s*(.+)$", re.MULTILINE)


class LinearError(RuntimeError):
    """A GraphQL error, transport error, or unexpected response shape."""


@contextmanager
def _linear_errors(what: str) -> Generator[None]:
    """Funnel every way an adapter call can fail into ``LinearError``.

    The SDK raises ``LinearError`` subclasses for anything the API *answered*,
    a bare ``ValueError`` when a 200 carries no issue, and ``ValidationError``
    when a response does not match the generated model. Transport failures are
    not wrapped at all and arrive as ``httpx.HTTPError``. Collapsing all four
    here is what keeps ``main``'s single except clause -- and the exit-code-1
    contract -- honest.

    A context manager rather than a ``_call(thunk)`` helper so the call site
    stays a plain call and keeps its inferred return type.
    """
    try:
        yield
    except (_SdkLinearError, httpx.HTTPError, ValidationError, ValueError) as exc:
        msg = f"{what}: {type(exc).__name__}: {str(exc)[:500]}"
        raise LinearError(msg) from exc


def resolve_team_id(team: str, api_key: str) -> str:
    """Return a team UUID, accepting either a UUID or a team key."""
    if _UUID_RE.match(team):
        return team
    with _linear_errors(f"resolving Linear team {team!r}"):
        resolved = linear.get_team_by_key(team, api_key=api_key)
    if resolved is None:
        msg = (
            f"No Linear team with key {team!r}. Pass --team a team key that "
            f"exists, or the team's UUID."
        )
        raise LinearError(msg)
    return str(resolved.id)


def find_existing_issue(
    team_id: str,
    marker: str,
    api_key: str,
) -> Issue | None:
    """Return the newest un-finished issue carrying ``marker``, if any.

    Filtering happens client-side on the marker: Linear's ``description``
    comparator support has shifted over time, whereas team + state filtering is
    stable. 100 issues is a generous window for open CI-triage tickets.
    """
    with _linear_errors("listing open Linear issues"):
        issues = linear.list_open_team_issues(team_id, first=100, api_key=api_key)
    for issue in issues:
        if marker in (issue.description or ""):
            return issue
    return None


def build_footer(
    *,
    marker: str,
    occurrences: int,
    run_url: str,
    branch: str,
    commit: str,
    first_seen: str,
    latest_seen: str,
) -> str:
    """Render the script-managed metadata block."""
    return "\n".join(
        [
            FOOTER_SENTINEL,
            "---",
            "**CI triage metadata** — managed by "
            "`scripts/ci-triage-linear-issue.py`. Edit above this line only.",
            "",
            f"- Occurrences: {occurrences}",
            f"- Latest: {run_url}",
            f"- Latest branch / commit: `{branch}` @ `{commit}`",
            f"- First seen: {first_seen}",
            f"- Last seen: {latest_seen}",
            "",
            marker,
        ],
    )


def split_body(description: str) -> str:
    """Return the human-owned part of a description, dropping the footer."""
    return description.split(FOOTER_SENTINEL, 1)[0].rstrip()


def create_issue(
    *,
    team_id: str,
    title: str,
    description: str,
    api_key: str,
) -> Issue:
    with _linear_errors("creating the Linear issue"):
        return linear.create_issue(
            IssueCreateInput(team_id=team_id, title=title, description=description),
            api_key=api_key,
        )


def update_description(
    *,
    issue_id: str,
    description: str,
    api_key: str,
) -> None:
    with _linear_errors(f"updating Linear issue {issue_id}"):
        linear.update_issue(
            issue_id,
            IssueUpdateInput(description=description),
            api_key=api_key,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", required=True, help="Failing workflow name")
    parser.add_argument("--run-url", required=True, help="Failing run's HTML URL")
    parser.add_argument("--branch", default="unknown", help="Head branch")
    parser.add_argument("--commit", default="unknown", help="Head SHA")
    parser.add_argument(
        "--diagnosis-file",
        required=True,
        type=Path,
        help="Markdown diagnosis produced by the triage agent",
    )
    parser.add_argument(
        "--timestamp",
        default="",
        help="ISO timestamp for this occurrence (defaults to the run URL only)",
    )
    parser.add_argument(
        "--team",
        default=LINEAR_TEAM,
        help=f"Linear team key or UUID (default: {LINEAR_TEAM})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional file to append 'identifier<TAB>url' to (e.g. a step summary)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    api_key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not api_key:
        print("error: LINEAR_API_KEY must be set", file=sys.stderr)
        return 2
    team = args.team.strip()

    if not args.diagnosis_file.is_file():
        print(
            f"notice: no diagnosis at {args.diagnosis_file}; nothing to file.",
            file=sys.stderr,
        )
        return 0
    diagnosis = args.diagnosis_file.read_text(encoding="utf-8").strip()
    if not diagnosis:
        print("notice: diagnosis file is empty; nothing to file.", file=sys.stderr)
        return 0

    marker = MARKER_TEMPLATE.format(workflow=args.workflow)
    stamp = args.timestamp or args.run_url

    try:
        team_id = resolve_team_id(team, api_key)
        existing = find_existing_issue(team_id, marker, api_key)

        if existing is None:
            body = "\n\n".join(
                [
                    diagnosis,
                    build_footer(
                        marker=marker,
                        occurrences=1,
                        run_url=args.run_url,
                        branch=args.branch,
                        commit=args.commit,
                        first_seen=stamp,
                        latest_seen=stamp,
                    ),
                ],
            )
            issue = create_issue(
                team_id=team_id,
                title=f"ci: {args.workflow} is failing",
                description=body,
                api_key=api_key,
            )
            action = "created"
        else:
            previous = existing.description or ""
            occurrence_match = _OCCURRENCES_RE.search(previous)
            occurrences = int(occurrence_match.group(1)) + 1 if occurrence_match else 2
            first_match = _FIRST_SEEN_RE.search(previous)
            first_seen = first_match.group(1).strip() if first_match else stamp
            body = "\n\n".join(
                [
                    split_body(previous) or diagnosis,
                    build_footer(
                        marker=marker,
                        occurrences=occurrences,
                        run_url=args.run_url,
                        branch=args.branch,
                        commit=args.commit,
                        first_seen=first_seen,
                        latest_seen=stamp,
                    ),
                ],
            )
            update_description(
                issue_id=str(existing.id),
                description=body,
                api_key=api_key,
            )
            issue = existing
            action = "updated"
    except LinearError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    identifier = str(issue.identifier)
    url = str(issue.url)
    print(f"{action} {identifier}: {url}")
    if args.output is not None:
        with args.output.open("a", encoding="utf-8") as handle:
            handle.write(f"{identifier}\t{url}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
