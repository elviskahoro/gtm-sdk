#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""File (or bump) a Linear issue describing a failed CI run.

Reads a diagnosis written by the triage agent and turns it into exactly one
Linear issue per distinct failing workflow. A recurring failure updates the
existing issue's metadata footer instead of filing a duplicate -- the nightly
Integration tests were red 13 nights running (2026-07-15..2026-07-27), and that
is one issue, not thirteen.

Why stdlib-only, and why it does not import ``libs/linear``:

This runs in the CI job that reacts to a broken build, so it must not depend on
the repo's environment resolving. ``uv sync`` is itself a plausible cause of the
failure being triaged -- if the triage tool needs the dependency graph to
install, it dies in precisely the case it exists to report on. ``libs/linear``
also buys little here: its ``IssueCreateInput`` only carries title/teamId/
description, which is the same surface this script needs, and it cannot post
comments. Same reasoning and precedent as ``scripts/docs-pages-lint.py``, which
is deliberately standalone so CI can call it before ``uv sync`` finishes.

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
from scripts.lib.uv_bootstrap import bootstrap_uv as _bootstrap_uv  # noqa: E402

if __name__ == "__main__":
    _bootstrap_uv(script_path=__file__, mode="script")

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LINEAR_API_URL = "https://api.linear.app/graphql"

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
_OCCURRENCES_RE = re.compile(r"^- Occurrences:\s*(\d+)", re.MULTILINE)
_FIRST_SEEN_RE = re.compile(r"^- First seen:\s*(.+)$", re.MULTILINE)


class LinearError(RuntimeError):
    """A GraphQL error, transport error, or unexpected response shape."""


def _graphql(query: str, variables: dict[str, Any], api_key: str) -> dict[str, Any]:
    """POST a GraphQL document and return its ``data`` payload.

    Linear takes the raw personal API key as the Authorization header with no
    ``Bearer`` prefix -- adding one yields an opaque 400.
    """
    request = urllib.request.Request(  # noqa: S310 - fixed https endpoint
        LINEAR_API_URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": api_key,
        },
        method="POST",
    )
    try:
        # LINEAR_API_URL is a module constant with a literal https scheme, so no
        # caller-supplied URL or alternate scheme can reach urlopen.
        # trunk-ignore(bandit/B310)
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:  # pragma: no cover - network failure
        body = exc.read().decode(errors="replace")[:500]
        msg = f"Linear API returned HTTP {exc.code}: {body}"
        raise LinearError(msg) from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network failure
        msg = f"Could not reach the Linear API: {exc.reason}"
        raise LinearError(msg) from exc

    if errors := payload.get("errors"):
        msg = f"Linear GraphQL errors: {json.dumps(errors)[:500]}"
        raise LinearError(msg)
    data = payload.get("data")
    if not isinstance(data, dict):
        msg = f"Unexpected Linear response shape: {str(payload)[:300]}"
        raise LinearError(msg)
    return data


def resolve_team_id(team: str, api_key: str) -> str:
    """Return a team UUID, accepting either a UUID or a team key."""
    if _UUID_RE.match(team):
        return team
    query = """
      query ResolveTeam($key: String!) {
        teams(filter: { key: { eq: $key } }, first: 1) { nodes { id key } }
      }
    """
    nodes = _graphql(query, {"key": team}, api_key)["teams"]["nodes"]
    if not nodes:
        msg = (
            f"No Linear team with key {team!r}. Set LINEAR_TEAM_ID to a team key "
            f"that exists, or to the team's UUID."
        )
        raise LinearError(msg)
    return str(nodes[0]["id"])


def find_existing_issue(
    team_id: str,
    marker: str,
    api_key: str,
) -> dict[str, Any] | None:
    """Return the newest un-finished issue carrying ``marker``, if any.

    Filtering happens client-side on the marker: Linear's ``description``
    comparator support has shifted over time, whereas team + state filtering is
    stable. 100 issues is a generous window for open CI-triage tickets.
    """
    query = """
      query FindTriageIssue($teamId: ID!) {
        issues(
          filter: {
            team: { id: { eq: $teamId } }
            state: { type: { nin: ["completed", "canceled"] } }
          }
          first: 100
          orderBy: updatedAt
        ) {
          nodes { id identifier url title description }
        }
      }
    """
    nodes = _graphql(query, {"teamId": team_id}, api_key)["issues"]["nodes"]
    for node in nodes:
        if marker in (node.get("description") or ""):
            return node
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
) -> dict[str, Any]:
    query = """
      mutation CreateTriageIssue($input: IssueCreateInput!) {
        issueCreate(input: $input) {
          success
          issue { id identifier url }
        }
      }
    """
    variables = {
        "input": {"teamId": team_id, "title": title, "description": description},
    }
    result = _graphql(query, variables, api_key)["issueCreate"]
    if not result.get("success") or not result.get("issue"):
        msg = f"Linear declined to create the issue: {json.dumps(result)[:300]}"
        raise LinearError(msg)
    return dict(result["issue"])


def update_description(
    *,
    issue_id: str,
    description: str,
    api_key: str,
) -> None:
    query = """
      mutation BumpTriageIssue($id: String!, $input: IssueUpdateInput!) {
        issueUpdate(id: $id, input: $input) { success }
      }
    """
    result = _graphql(
        query,
        {"id": issue_id, "input": {"description": description}},
        api_key,
    )["issueUpdate"]
    if not result.get("success"):
        msg = f"Linear declined to update issue {issue_id}"
        raise LinearError(msg)


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
            previous = existing.get("description") or ""
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
                issue_id=str(existing["id"]),
                description=body,
                api_key=api_key,
            )
            issue = existing
            action = "updated"
    except LinearError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    identifier = str(issue.get("identifier", "?"))
    url = str(issue.get("url", ""))
    print(f"{action} {identifier}: {url}")
    if args.output is not None:
        with args.output.open("a", encoding="utf-8") as handle:
            handle.write(f"{identifier}\t{url}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
