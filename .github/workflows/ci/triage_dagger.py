"""Dagger pipeline: file a Linear issue describing a failed CI run.

Wraps ``scripts/ci-triage-linear-issue.py`` in a pinned container so the filing
step is reproducible and its credential never touches the runner's shell. Invoked
the same way locally and in CI. Locally, let ``uv run`` provision and select the
project's environment -- a bare ``python`` resolves via ``$PATH`` (e.g. a pyenv
shim), not this repo's ``uv``-managed venv where ``dagger-io``/``anyio`` live:

    uv run dagger run python .github/workflows/ci/triage_dagger.py \\
      --workflow "Unit tests" \\
      --run-url https://github.com/o/r/actions/runs/123 \\
      --branch main --commit abc1234 \\
      --diagnosis-file tmp/diagnosis.md \\
      --output tmp/linear-issue.tsv

CI resolves ``python`` to a dedicated dagger-io/anyio venv via ``$GITHUB_PATH``
(see ``.github/workflows/agent-ci-triage.yml``), so the CI invocation stays a bare
``dagger run python "${pipeline}"``.

Why the container is deliberately tiny: it installs exactly one pinned wheel and
mounts exactly two paths -- the filing script and the ``libs/linear`` adapter it
calls. No ``uv sync``, no lockfile, no repo dependency graph, so a broken
lockfile or a poisoned working tree still cannot influence the filing step. That
matters here more than usual: this pipeline runs *because* something in CI is
already broken.

The ``pip install`` is its own ``with_exec``, placed before the mounts so the
layer caches on the base image and the pin alone -- a diagnosis file changing
every run must not re-resolve the dependency. Keep ``GTM_LINEAR_PIN`` in lockstep
with the PEP 723 header in ``scripts/ci-triage-linear-issue.py``; a test asserts
they agree. Same shape as ``triage_diagnose_dagger.py``'s ``OZ_SDK_PIN``.

The Linear key is forwarded as a Dagger secret, so it lands as an env var the
script reads without being baked into an image layer or echoed into the build
log. The target Linear team is hard-coded in the script, not passed in.

Exit code contract, mirroring ``pytest_integration_dagger.py``: the wrapped
command's status is captured into a file so the ``with_exec`` itself stays green
and the output file remains exportable, then re-raised after the export. 0 means
filed, bumped, or deliberately skipped (no diagnosis to file); non-zero means a
real misconfiguration or API error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import anyio
import dagger
from dagger import dag

CONTAINER_IMAGE = "python:3.13-slim"

# Keep in lockstep with the PEP 723 header in the filing script and with the
# project's floor -- tests/workflows/test_triage_workflow.py enforces all three.
GTM_LINEAR_PIN = "gtm-linear==0.1.0"

WORKDIR = "/work"
SCRIPT_NAME = "ci-triage-linear-issue.py"
SCRIPT_IN_CONTAINER = f"{WORKDIR}/scripts/{SCRIPT_NAME}"
DIAGNOSIS_IN_CONTAINER = f"{WORKDIR}/diagnosis.md"
OUTPUT_IN_CONTAINER = f"{WORKDIR}/linear-issue.tsv"
RC_IN_CONTAINER = f"{WORKDIR}/triage_rc"

# The trailing `echo $? > …` always exits 0 so the `with_exec` succeeds and the
# output file stays exportable; main() reads the real code back and re-raises it.
# Do NOT collapse this into a bare command or a `|| true` (see ai-eun for the
# swallowed-exit-code failure mode this pattern exists to prevent).
RC_SUFFIX = f"; echo $? > {RC_IN_CONTAINER}"


def build_container(
    *,
    api_key: str,
    diagnosis_host_path: Path,
    script_args: list[str],
) -> dagger.Container:
    """Build the filing container. Caller must be inside ``dagger.connection(...)``."""
    scripts = dag.host().directory("scripts", include=[SCRIPT_NAME])
    # The adapter, and only the adapter. `libs/linear/client.py` imports nothing
    # but stdlib at runtime (every `gtm_linear` import is lazy or TYPE_CHECKING),
    # so this pulls in two small files rather than the `libs/` tree.
    libs = dag.host().directory("libs", include=["__init__.py", "linear/**"])
    diagnosis = dag.host().file(str(diagnosis_host_path))
    secret = dag.set_secret("linear-api-key", api_key)

    command = " ".join(
        ["python3", SCRIPT_IN_CONTAINER, *(_quote(arg) for arg in script_args)],
    )

    return (
        dag.container()
        .from_(CONTAINER_IMAGE)
        .with_exec(["pip", "install", "--no-cache-dir", "--quiet", GTM_LINEAR_PIN])
        .with_directory(f"{WORKDIR}/scripts", scripts)
        .with_directory(f"{WORKDIR}/libs", libs)
        .with_file(DIAGNOSIS_IN_CONTAINER, diagnosis)
        .with_secret_variable("LINEAR_API_KEY", secret)
        .with_workdir(WORKDIR)
        .with_exec(["sh", "-c", command + RC_SUFFIX])
    )


def _quote(arg: str) -> str:
    """Single-quote an argument for the `sh -c` wrapper.

    Run metadata (branch names, workflow names) reaches this from a GitHub event
    payload, so it is untrusted text that must not be able to break out of the
    command string.
    """
    return "'" + arg.replace("'", "'\\''") + "'"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--branch", default="unknown")
    parser.add_argument("--commit", default="unknown")
    parser.add_argument("--timestamp", default="")
    parser.add_argument(
        "--diagnosis-file",
        required=True,
        type=Path,
        help="Host path to the agent's diagnosis markdown",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Host path to write 'identifier<TAB>url' to",
    )
    return parser.parse_args(argv)


def script_args_from(args: argparse.Namespace) -> list[str]:
    """Translate host-side args into the in-container invocation."""
    return [
        "--workflow",
        args.workflow,
        "--run-url",
        args.run_url,
        "--branch",
        args.branch,
        "--commit",
        args.commit,
        "--timestamp",
        args.timestamp,
        "--diagnosis-file",
        DIAGNOSIS_IN_CONTAINER,
        "--output",
        OUTPUT_IN_CONTAINER,
    ]


async def main() -> None:
    args = parse_args()

    api_key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write("LINEAR_API_KEY is not set in the environment\n")
        sys.exit(2)

    if not args.diagnosis_file.is_file():
        # Not an error: the agent step is best-effort, and a missing diagnosis must
        # not turn one red check into two.
        sys.stderr.write(
            f"notice: no diagnosis at {args.diagnosis_file}; nothing to file.\n",
        )
        sys.exit(0)

    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        ctr = build_container(
            api_key=api_key,
            diagnosis_host_path=args.diagnosis_file,
            script_args=script_args_from(args),
        )

        # Read the wrapped command's real status first, so a missing output file is
        # interpretable. Any failure to read or parse it (killed container, empty
        # value, non-integer) fails closed at rc=1: red with a controlled message
        # rather than a traceback.
        rc_host = args.output.parent / "triage_rc"
        try:
            await ctr.file(RC_IN_CONTAINER).export(str(rc_host))
            rc = int(rc_host.read_text().strip())
        except (dagger.DaggerError, OSError, ValueError) as exc:
            sys.stderr.write(
                f"warning: could not read the triage exit code from "
                f"{RC_IN_CONTAINER} ({exc}); failing closed at rc=1\n",
            )
            rc = 1

        # The script writes no output file when it deliberately skips, so a missing
        # file is only fatal if the script also claimed success.
        try:
            await ctr.file(OUTPUT_IN_CONTAINER).export(str(args.output))
        except dagger.DaggerError as exc:
            sys.stderr.write(
                f"notice: no {OUTPUT_IN_CONTAINER} to export "
                f"(triage exited {rc}): {exc}\n",
            )

    if rc != 0:
        sys.stderr.write(f"linear triage exited {rc}\n")
    sys.exit(rc)


if __name__ == "__main__":
    anyio.run(main)
