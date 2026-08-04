r"""Dagger pipeline: file a Linear issue describing a failed CI run.

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
calls. No ``uv sync``, no ``uv.lock``, no repo dependency graph, so a broken
lockfile or a poisoned working tree still cannot influence the filing step. That
matters here more than usual: this pipeline runs *because* something in CI is
already broken.

This pipeline intentionally stays on a digest-pinned minimal Python image rather
than the Flox toolchain image. Its isolation from the repository dependency graph
is part of the failure-triage contract.

The ``pip install`` is its own ``with_exec``, placed before the mounts so the
layer caches on the base image and the pin alone -- a diagnosis file changing
every run must not re-resolve the dependency. It installs with
``--require-hashes`` against the committed ``constraints/gtm-linear.txt``, so the
full transitive closure (httpx, pydantic, ...), not just the top-level pin, is
hash-verified rather than resolved fresh from PyPI on every triage run. Keep
``GTM_LINEAR_PIN`` in lockstep with the PEP 723 header in
``scripts/ci-triage-linear-issue.py`` and the first line of the constraints
file; a test asserts all three agree. Regenerate the constraints file with the
command in its own header comment whenever the pin changes. Same shape as
``triage_diagnose_dagger.py``'s ``OZ_SDK_PIN``.

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

CONTAINER_IMAGE = (
    "python:3.13-slim@sha256:"
    "6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91"
)

# Keep in lockstep with the PEP 723 header in the filing script, the project's
# floor, and the first requirement line of CONSTRAINTS_FILE --
# tests/workflows/test_triage_workflow.py enforces all four.
GTM_LINEAR_PIN = "gtm-linear==0.1.0"

WORKDIR = "/work"
SCRIPT_NAME = "ci-triage-linear-issue.py"
SCRIPT_IN_CONTAINER = f"{WORKDIR}/scripts/{SCRIPT_NAME}"
DIAGNOSIS_IN_CONTAINER = f"{WORKDIR}/diagnosis.md"
OUTPUT_IN_CONTAINER = f"{WORKDIR}/linear-issue.tsv"
RC_IN_CONTAINER = f"{WORKDIR}/triage_rc"

# Hash-locked closure for GTM_LINEAR_PIN, regenerated via the command in its
# own header comment. `--require-hashes` refuses to install anything --
# including a transitive dependency -- that isn't listed here by hash.
CONSTRAINTS_FILE = Path(__file__).parent / "constraints" / "gtm-linear.txt"
CONSTRAINTS_IN_CONTAINER = f"{WORKDIR}/constraints.txt"

# The trailing `echo $? > …` always exits 0 so the `with_exec` succeeds and the
# output file stays exportable; main() reads the real code back and re-raises it.
# Do NOT collapse this into a bare command or a `|| true` (see ai-eun for the
# swallowed-exit-code failure mode this pattern exists to prevent).
RC_SUFFIX = f"; echo $? > {RC_IN_CONTAINER}"


def _assert_constraints_pin_matches() -> None:
    """Fail fast if CONSTRAINTS_FILE drifts from GTM_LINEAR_PIN.

    The pin-agreement test catches this too, but that only runs in CI; this
    catches it the moment someone bumps the pin locally without regenerating
    the constraints file. `pip-compile` output is sorted alphabetically, so
    the pin is not necessarily the first requirement line.
    """
    package_name = GTM_LINEAR_PIN.split("==", 1)[0]
    lines = CONSTRAINTS_FILE.read_text(encoding="utf-8").splitlines()
    matches = [line for line in lines if line.startswith(f"{package_name}==")]
    if not matches or matches[0].split(" \\", 1)[0].strip() != GTM_LINEAR_PIN:
        msg = (
            f"{CONSTRAINTS_FILE} does not pin {GTM_LINEAR_PIN!r}. Regenerate "
            "the constraints file (see its header comment for the command)."
        )
        raise AssertionError(msg)


def build_container(
    *,
    api_key: str,
    diagnosis_host_path: Path,
    script_args: list[str],
) -> dagger.Container:
    """Build the filing container. Caller must be inside ``dagger.connection(...)``."""
    _assert_constraints_pin_matches()
    scripts = dag.host().directory("scripts", include=[SCRIPT_NAME])
    # The adapter, and only the adapter. `libs/linear/client.py` imports nothing
    # but stdlib at runtime (every `gtm_linear` import is lazy or TYPE_CHECKING),
    # so this pulls in two small files rather than the `libs/` tree.
    libs = dag.host().directory("libs", include=["__init__.py", "linear/**"])
    diagnosis = dag.host().file(str(diagnosis_host_path))
    constraints = dag.host().file(str(CONSTRAINTS_FILE))
    secret = dag.set_secret("linear-api-key", api_key)

    command = " ".join(
        ["python3", SCRIPT_IN_CONTAINER, *(_quote(arg) for arg in script_args)],
    )

    return (
        dag.container()
        .from_(CONTAINER_IMAGE)
        .with_file(CONSTRAINTS_IN_CONTAINER, constraints)
        # GTM_LINEAR_PIN is pinned via CONSTRAINTS_FILE; --require-hashes refuses
        # anything not listed there by hash, closure included.
        .with_exec(
            [
                "pip",
                "install",
                "--no-cache-dir",
                "--quiet",
                "--require-hashes",
                "-r",
                CONSTRAINTS_IN_CONTAINER,
            ],
        )
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
