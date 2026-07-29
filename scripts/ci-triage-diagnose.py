#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["oz-agent-sdk==0.14.0", "httpx>=0.23.0"]
# ///
"""Ask an Oz cloud agent why a CI run failed, and write its findings to a file.

Runs the agent through ``oz_agent_sdk`` (Warp's REST API) rather than
``warpdotdev/oz-agent-action``. Two reasons: the SDK is plain Python, so the whole
triage pipeline fits inside a Dagger container instead of needing a `uses:` step on
the runner; and this repo's own guidance is to prefer cloud agent runs over local
execution.

The trade-off that shapes this file: a cloud run executes in Warp's sandbox, NOT on
our runner, so it cannot read our checkout. Handing it a repo path would be worse
than useless -- ``git show`` would still succeed against whatever the sandbox
happens to have cloned and the agent would diagnose confidently against the wrong
bytes. So all context is embedded in the prompt by the caller: the failed-step log
tail and the diff that introduced the failure, both extracted on the runner where
the real commit is available. The agent is told it has no filesystem.

Getting text back out is the other constraint. ``RunItem`` exposes no final-message
field -- artifacts are the only channel. Three are tried in order:

1. A PLAN artifact, whose ``get_artifact`` response carries ``data.content`` as
   markdown directly. This is why the run uses ``mode="plan"``: a diagnosis is
   plan-shaped, and it is the one output path that does not depend on the sandbox
   successfully writing and uploading a file.
2. A FILE artifact named like the requested output, fetched from its signed
   ``download_url``.
3. Nothing usable -> exit 0 having written no file. The caller treats that as
   "no diagnosis" and still files a bare issue, because a red check must produce a
   ticket whether or not the agent cooperated.

Usage:

    WARP_API_KEY=... scripts/ci-triage-diagnose.py \\
        --workflow "Unit tests" \\
        --run-url https://github.com/o/r/actions/runs/123 \\
        --log-file tmp/ci-failure-tail.log \\
        --diff-file tmp/failing-diff.patch \\
        --output tmp/diagnosis.md
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

# Terminal states from RunState. BLOCKED means the agent is waiting on input that
# will never arrive in CI, so it is terminal for our purposes.
TERMINAL_STATES = frozenset(
    {"SUCCEEDED", "FAILED", "ERROR", "CANCELLED", "BLOCKED"},
)
SUCCESS_STATES = frozenset({"SUCCEEDED"})

# Prompt budget. The log tail is capped again here (the caller also caps it) because
# a cloud run carries the whole prompt over the wire and an unbounded lint log can
# be megabytes.
LOG_BUDGET_BYTES = 30_000
DIFF_BUDGET_BYTES = 20_000

POLL_INTERVAL_SECONDS = 10
DEFAULT_TIMEOUT_SECONDS = 900

PROMPT_TEMPLATE = """\
A CI run in {repo} just failed. Work out the root cause and write it up.

You are running as a cloud agent. You have NO access to the repository working
tree and NO credentials -- do not try to clone, check out, run git, use `gh`, or
call any API. Everything you need is in this prompt. If the evidence here is not
enough to reach a conclusion, say so plainly instead of guessing.

Failing run:
  workflow:  {workflow}
  run URL:   {run_url}
  branch:    {branch}
  commit:    {commit}
  triggered: {event}

## Your output

Markdown, no top-level heading, under roughly 400 words, with three sections:

1. **Root cause** -- one paragraph. Quote the exact failing assertion, lint code,
   or error line. Name the file and line when the evidence points at one.
2. **Fix** -- the concrete change you would make. Describe it; do not attempt to
   apply it.
3. **Confidence** -- one line: high, medium, or low, plus what would raise it.

Do not restate the run metadata; it is added automatically. Do not write a title.
Never include a `Co-Authored-By:` trailer -- this repo's hooks reject agent
identities and the rule covers prose too.

## Repository context

- Linters and formatters run only through trunk (`trunk check --filter=<tool>`),
  never as bare binaries: ruff, mypy, pyright, actionlint, checkov, yamllint,
  taplo, shellcheck, semgrep, tach.
- `tach` enforces module boundaries: `libs/<x>` must never import from `libs/<y>`;
  cross-adapter coordination belongs in `src/`.
- Package manager is `uv`, never pip. `uv sync --frozen` respects uv.lock, so a
  lockfile/manifest mismatch surfaces there first.
- Unit and Integration tests run pytest inside a Dagger container on a Namespace
  ARM runner. Failures are sometimes infrastructure rather than code: stale
  Namespace cache generations, registry.dagger.io pull brownouts, and engine
  readiness races have all produced red runs. Say so when the evidence points that
  way rather than inventing a code cause.
- Integration tests run nightly on a schedule and read live credentials from the
  environment. Scheduled runs have been failing while manual `workflow_dispatch`
  runs of the same suite pass; if this is that failure, the trigger difference is
  the most important clue.
- `docs/cli/` is generated by `scripts/docs-cli_reference-generate.py`.

## Failed-step log (tail)

```
{log}
```
{diff_section}"""

DIFF_SECTION_TEMPLATE = """
## Diff that introduced the failure

This is `git diff` between the default branch and the failing commit, extracted on
the runner. It is the authoritative view of what changed -- you cannot read the
repository yourself.

```diff
{diff}
```
"""


def _read_capped(path: Path | None, budget: int) -> str:
    """Return the tail of a file, or '' when absent/empty.

    The tail rather than the head: the actual error is at the end of a log.
    """
    if path is None or not path.is_file():
        return ""
    raw = path.read_bytes()
    if len(raw) > budget:
        raw = raw[-budget:]
    return raw.decode("utf-8", errors="replace").strip()


def build_prompt(
    *,
    repo: str,
    workflow: str,
    run_url: str,
    branch: str,
    commit: str,
    event: str,
    log: str,
    diff: str,
) -> str:
    diff_section = DIFF_SECTION_TEMPLATE.format(diff=diff) if diff else ""
    return PROMPT_TEMPLATE.format(
        repo=repo,
        workflow=workflow,
        run_url=run_url,
        branch=branch,
        commit=commit,
        event=event,
        log=log or "(no failed-step log was captured)",
        diff_section=diff_section,
    )


def poll_until_terminal(
    client: Any,
    run_id: str,
    *,
    timeout_seconds: int,
    interval_seconds: int = POLL_INTERVAL_SECONDS,
    sleep: Any = time.sleep,
    now: Any = time.monotonic,
) -> Any:
    """Poll a run until it reaches a terminal state or the deadline passes.

    Returns the last observed run. ``sleep``/``now`` are injectable so tests do not
    spend real seconds.
    """
    deadline = now() + timeout_seconds
    run = None
    while True:
        run = client.agent.runs.retrieve(run_id)
        state = getattr(run, "state", None)
        if state in TERMINAL_STATES:
            return run
        if now() >= deadline:
            sys.stderr.write(
                f"warning: run {run_id} still {state} after {timeout_seconds}s; "
                f"giving up on a diagnosis\n",
            )
            return run
        sleep(interval_seconds)


def extract_diagnosis(
    client: Any,
    run: Any,
    *,
    want_filename: str,
    fetch_url: Any,
) -> str:
    """Pull markdown out of a finished run's artifacts.

    Tries a PLAN artifact first (its response embeds the markdown), then a FILE
    artifact matching ``want_filename`` via its signed download URL. Returns '' when
    neither yields anything -- the caller must tolerate that.
    """
    artifacts = list(getattr(run, "artifacts", None) or [])

    plans = [a for a in artifacts if getattr(a, "artifact_type", None) == "PLAN"]
    files = [a for a in artifacts if getattr(a, "artifact_type", None) == "FILE"]

    for artifact in plans:
        uid = _artifact_uid(artifact)
        if uid is None:
            continue
        try:
            response = client.agent.get_artifact(uid)
            content = getattr(getattr(response, "data", None), "content", "") or ""
        except Exception as exc:  # noqa: BLE001 - any SDK/transport error is non-fatal
            sys.stderr.write(f"warning: could not read plan artifact {uid}: {exc}\n")
            continue
        if content.strip():
            return content.strip()

    # Prefer an exact filename match, but accept any markdown file the agent left.
    def _rank(artifact: Any) -> int:
        name = str(getattr(getattr(artifact, "data", None), "filename", "") or "")
        if name == want_filename:
            return 0
        return 1 if name.endswith(".md") else 2

    for artifact in sorted(files, key=_rank):
        if _rank(artifact) == 2:
            continue
        uid = _artifact_uid(artifact)
        if uid is None:
            continue
        try:
            response = client.agent.get_artifact(uid)
            url = getattr(getattr(response, "data", None), "download_url", "") or ""
            if not url:
                continue
            content = fetch_url(url)
        except Exception as exc:  # noqa: BLE001 - non-fatal, fall through
            sys.stderr.write(f"warning: could not read file artifact {uid}: {exc}\n")
            continue
        if content.strip():
            return content.strip()

    return ""


def _artifact_uid(artifact: Any) -> str | None:
    data = getattr(artifact, "data", None)
    uid = getattr(data, "artifact_uid", None)
    return str(uid) if uid else None


def _fetch_url(url: str) -> str:
    import httpx

    response = httpx.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    return response.text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose a failed CI run via Oz.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "?"))
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--branch", default="unknown")
    parser.add_argument("--commit", default="unknown")
    parser.add_argument("--event", default="unknown")
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--diff-file", type=Path, default=None)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--environment-id",
        default=os.environ.get("OZ_ENVIRONMENT_ID", ""),
        help="Optional Warp environment UID; cloud runs use none when unset",
    )
    parser.add_argument(
        "--harness",
        default=os.environ.get("OZ_HARNESS", ""),
        help="Optional harness type: oz, claude, gemini, or codex",
    )
    parser.add_argument("--model-id", default=os.environ.get("OZ_MODEL_ID", ""))
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--list-environments",
        action="store_true",
        help="Print the environments this key can see, then exit. Use this to "
        "discover a value for --environment-id.",
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> dict[str, Any]:
    """Assemble the cloud-run config, omitting anything unset.

    Empty strings are dropped rather than sent: the API treats an absent
    environment_id as "no environment", which is the documented default.
    """
    config: dict[str, Any] = {"name": f"ci-triage-{args.workflow}"}
    if args.environment_id:
        config["environment_id"] = args.environment_id
    if args.harness:
        config["harness"] = {"type": args.harness}
    if args.model_id:
        config["model_id"] = args.model_id
    return config


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    args = parse_args(argv)

    if client is None:
        if not os.environ.get("WARP_API_KEY", "").strip():
            print("error: WARP_API_KEY must be set", file=sys.stderr)
            return 2
        # oz-agent-sdk is intentionally not a project dependency: it is installed
        # only inside the Dagger container (and by this file's PEP 723 header for
        # direct runs), which is why the import is function-local.
        # trunk-ignore(pyrefly/missing-import)
        from oz_agent_sdk import OzAPI

        client = OzAPI()

    if args.list_environments:
        try:
            print(client.agent.list_environments())
        except Exception as exc:  # noqa: BLE001 - diagnostic path
            print(f"error: could not list environments: {exc}", file=sys.stderr)
            return 1
        return 0

    prompt = build_prompt(
        repo=args.repo,
        workflow=args.workflow,
        run_url=args.run_url,
        branch=args.branch,
        commit=args.commit,
        event=args.event,
        log=_read_capped(args.log_file, LOG_BUDGET_BYTES),
        diff=_read_capped(args.diff_file, DIFF_BUDGET_BYTES),
    )

    try:
        started = client.agent.run(
            prompt=prompt,
            # plan mode so the result comes back as a PLAN artifact, whose response
            # embeds markdown. See the module docstring.
            mode="plan",
            interactive=False,
            title=f"CI triage: {args.workflow}",
            config=build_config(args),
        )
    except Exception as exc:  # noqa: BLE001 - surface any SDK error as a soft failure
        print(f"error: could not start an Oz run: {exc}", file=sys.stderr)
        return 1

    run_id = getattr(started, "run_id", "")
    if not run_id:
        print("error: Oz did not return a run_id", file=sys.stderr)
        return 1
    print(f"started Oz run {run_id} (state={getattr(started, 'state', '?')})")

    run = poll_until_terminal(
        client,
        run_id,
        timeout_seconds=args.timeout_seconds,
    )
    state = getattr(run, "state", None)
    print(f"run {run_id} finished in state {state}")

    diagnosis = extract_diagnosis(
        client,
        run,
        want_filename=args.output.name,
        fetch_url=_fetch_url,
    )

    if not diagnosis:
        # Deliberately not an error. The caller still files an issue from the log
        # alone -- a red check must produce a ticket even when the agent does not
        # cooperate.
        sys.stderr.write(
            f"notice: run {run_id} ({state}) produced no usable diagnosis "
            f"artifact; leaving {args.output} unwritten\n",
        )
        return 0

    if state not in SUCCESS_STATES:
        diagnosis += (
            f"\n\n> Note: the diagnosing agent finished in state `{state}`, so this "
            f"analysis may be incomplete.\n"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(diagnosis + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(diagnosis)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
