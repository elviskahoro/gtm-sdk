r"""Dagger pipeline: run an Oz cloud agent to diagnose a failed CI run.

Companion to ``triage_dagger.py`` (which files the result into Linear). Together
they put the whole triage flow in containers -- there is no ``uses:`` agent action
on the runner any more. Invoked the same way locally and in CI:

    uv run dagger run python .github/workflows/ci/triage_diagnose_dagger.py \\
      --workflow "Unit tests" \\
      --run-url https://github.com/o/r/actions/runs/123 \\
      --log-file tmp/ci-failure-tail.log \\
      --diff-file tmp/failing-diff.patch \\
      --output tmp/diagnosis.md

Unlike ``triage_dagger.py``, this container does install a dependency
(``oz-agent-sdk``), so the version is pinned here and nowhere else. It is still a
minimal image: no repo mount beyond ``scripts/``, no ``uv sync``, no ``uv.lock``, no
project dependency graph -- which matters because this runs precisely when CI is
broken. The install uses ``--require-hashes`` against the committed
``constraints/oz-agent-sdk.txt``, so the transitive closure is hash-verified too,
not just the top-level pin. Regenerate that file with the command in its own
header comment whenever ``OZ_SDK_PIN`` changes.

The agent executes on Warp's infrastructure, not in this container, so the
container never needs repo access. All evidence is passed in as files that the
caller extracted on the runner (log tail, and the diff between the default branch
and the failing commit). See the diagnose script's docstring for why handing a
cloud agent a repo path would be worse than giving it nothing.

Exit code contract mirrors ``pytest_integration_dagger.py``: the wrapped command's
status is captured to a file so the ``with_exec`` stays green and the output stays
exportable, then re-raised. 0 means "diagnosis written, or deliberately none";
non-zero means a misconfiguration or an Oz API failure.
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

# Pinned so an upstream SDK release cannot silently change agent behaviour with no
# diff in this repo. Keep in step with the PEP 723 block in
# scripts/ci-triage-diagnose.py.
OZ_SDK_PIN = "oz-agent-sdk==0.14.0"

WORKDIR = "/work"
SCRIPT_NAME = "ci-triage-diagnose.py"
SCRIPT_IN_CONTAINER = f"{WORKDIR}/scripts/{SCRIPT_NAME}"
LOG_IN_CONTAINER = f"{WORKDIR}/ci-failure-tail.log"
DIFF_IN_CONTAINER = f"{WORKDIR}/failing-diff.patch"
OUTPUT_IN_CONTAINER = f"{WORKDIR}/diagnosis.md"
RC_IN_CONTAINER = f"{WORKDIR}/diagnose_rc"

# Hash-locked closure for OZ_SDK_PIN, regenerated via the command in its own
# header comment. `--require-hashes` refuses to install anything -- including a
# transitive dependency -- that isn't listed here by hash.
CONSTRAINTS_FILE = Path(__file__).parent / "constraints" / "oz-agent-sdk.txt"
CONSTRAINTS_IN_CONTAINER = f"{WORKDIR}/constraints.txt"

# The trailing `echo $? > …` always exits 0, so the `with_exec` succeeds and the
# diagnosis stays exportable; main() reads the real code back and re-raises it. Do
# not collapse this into a bare command or a `|| true` (ai-eun).
RC_SUFFIX = f"; echo $? > {RC_IN_CONTAINER}"


def _quote(arg: str) -> str:
    """Single-quote an argument for the `sh -c` wrapper.

    Run metadata reaches this from a GitHub event payload, so it is untrusted text
    that must not be able to break out of the command string.
    """
    return "'" + arg.replace("'", "'\\''") + "'"


def _assert_constraints_pin_matches() -> None:
    """Fail fast if CONSTRAINTS_FILE drifts from OZ_SDK_PIN.

    The pin-agreement test catches this too, but that only runs in CI; this
    catches it the moment someone bumps the pin locally without regenerating
    the constraints file. `pip-compile` output is sorted alphabetically, so
    the pin is not necessarily the first requirement line.
    """
    package_name = OZ_SDK_PIN.split("==", 1)[0]
    lines = CONSTRAINTS_FILE.read_text(encoding="utf-8").splitlines()
    matches = [line for line in lines if line.startswith(f"{package_name}==")]
    if not matches or matches[0].split(" \\", 1)[0].strip() != OZ_SDK_PIN:
        msg = (
            f"{CONSTRAINTS_FILE} does not pin {OZ_SDK_PIN!r}. Regenerate "
            "the constraints file (see its header comment for the command)."
        )
        raise AssertionError(msg)


def build_container(
    *,
    api_key: str,
    log_host_path: Path | None,
    diff_host_path: Path | None,
    script_args: list[str],
) -> dagger.Container:
    """Build the diagnosing container. Caller must be inside ``dagger.connection``."""
    _assert_constraints_pin_matches()
    scripts = dag.host().directory("scripts", include=[SCRIPT_NAME])
    secret = dag.set_secret("warp-api-key", api_key)
    constraints = dag.host().file(str(CONSTRAINTS_FILE))

    ctr = (
        dag.container()
        .from_(CONTAINER_IMAGE)
        .with_file(CONSTRAINTS_IN_CONTAINER, constraints)
        # OZ_SDK_PIN is pinned via CONSTRAINTS_FILE; --require-hashes refuses
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
        .with_secret_variable("WARP_API_KEY", secret)
    )

    # Evidence files are optional: a cancelled run may have produced no log, and a
    # failure on the default branch has no meaningful diff.
    if log_host_path is not None and log_host_path.is_file():
        ctr = ctr.with_file(LOG_IN_CONTAINER, dag.host().file(str(log_host_path)))
    if diff_host_path is not None and diff_host_path.is_file():
        ctr = ctr.with_file(DIFF_IN_CONTAINER, dag.host().file(str(diff_host_path)))

    command = " ".join(
        ["python3", SCRIPT_IN_CONTAINER, *(_quote(arg) for arg in script_args)],
    )
    return ctr.with_workdir(WORKDIR).with_exec(["sh", "-c", command + RC_SUFFIX])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "?"))
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--branch", default="unknown")
    parser.add_argument("--commit", default="unknown")
    parser.add_argument("--event", default="unknown")
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--diff-file", type=Path, default=None)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    return parser.parse_args(argv)


def script_args_from(args: argparse.Namespace) -> list[str]:
    """Translate host-side args into the in-container invocation.

    Evidence paths are rewritten to their container locations, and omitted entirely
    when the host file is absent so the script's own "no log" handling kicks in.
    """
    out = [
        "--repo",
        args.repo,
        "--workflow",
        args.workflow,
        "--run-url",
        args.run_url,
        "--branch",
        args.branch,
        "--commit",
        args.commit,
        "--event",
        args.event,
        "--output",
        OUTPUT_IN_CONTAINER,
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    if args.log_file is not None and args.log_file.is_file():
        out += ["--log-file", LOG_IN_CONTAINER]
    if args.diff_file is not None and args.diff_file.is_file():
        out += ["--diff-file", DIFF_IN_CONTAINER]
    return out


async def main() -> None:
    args = parse_args()

    api_key = os.environ.get("WARP_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write("WARP_API_KEY is not set in the environment\n")
        sys.exit(2)

    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        ctr = build_container(
            api_key=api_key,
            log_host_path=args.log_file,
            diff_host_path=args.diff_file,
            script_args=script_args_from(args),
        )

        rc_host = args.output.parent / "diagnose_rc"
        try:
            await ctr.file(RC_IN_CONTAINER).export(str(rc_host))
            rc = int(rc_host.read_text().strip())
        except (dagger.DaggerError, OSError, ValueError) as exc:
            sys.stderr.write(
                f"warning: could not read the diagnose exit code from "
                f"{RC_IN_CONTAINER} ({exc}); failing closed at rc=1\n",
            )
            rc = 1

        # The script deliberately writes nothing when the agent produced no usable
        # artifact, so a missing file is expected, not fatal. The caller still files
        # an issue from the log alone.
        try:
            await ctr.file(OUTPUT_IN_CONTAINER).export(str(args.output))
        except dagger.DaggerError as exc:
            sys.stderr.write(
                f"notice: no diagnosis to export (diagnose exited {rc}): {exc}\n",
            )

    if rc != 0:
        sys.stderr.write(f"oz diagnose exited {rc}\n")
    sys.exit(rc)


if __name__ == "__main__":
    anyio.run(main)
