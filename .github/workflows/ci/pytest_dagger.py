"""Dagger pipeline: run pytest in a container and export the JUnit report.

Invoked the same way locally and in CI:

    dagger run python .github/workflows/ci/pytest_dagger.py

The pipeline runs pytest inside a python:3.13 container and exports `junit.xml`
to the host so a follow-up step (e.g. trunk-io/analytics-uploader) can upload
it. On the ARM64 8x16 Namespace runner, local measurements favored four
explicit xdist workers over both serial and eight workers: serial 13.16s, four
workers 10.74s, eight workers 12.08s.

Dependencies install with `uv sync --locked`: the run fails loudly if
`pyproject.toml` drifts from `uv.lock` instead of silently re-locking inside
CI — run `uv lock` after editing dependencies. (`--locked`, not `--frozen`:
frozen skips the freshness check and would happily install a stale lock.)

The pipeline *fails* (non-zero exit) when pytest exits non-zero, while still
exporting the report. A previous `... || true` swallowed pytest's exit code so
the job went green even on a failing suite (ai-eun); we instead capture pytest's
exit code into `/src/pytest_rc` (the trailing `echo` keeps the `with_exec` green
so the report stays exportable) and re-raise it after the export.
"""

from __future__ import annotations

import sys
from pathlib import Path
from time import perf_counter

import anyio
import dagger
from dagger import dag

# The trailing `echo $? > /src/pytest_rc` always exits 0, so the `with_exec`
# succeeds and `junit.xml` is guaranteed exportable; main() reads pytest_rc back
# and re-raises the real code. Do NOT restore a `|| true` here (see ai-eun).
PYTEST_CMD = (
    "uv run --no-sync pytest "
    "-p xdist.plugin -p pytest_asyncio.plugin -p anyio.pytest_plugin "
    "-n 4 --dist=loadfile "
    "--junit-xml=junit.xml -o junit_family=xunit1; "
    "echo $? > /src/pytest_rc"
)
JUNIT_HOST_PATH = "junit.xml"
PYTEST_RC_PATH = "/src/pytest_rc"
PYTEST_RC_HOST_PATH = "pytest_rc"

# Tests in tests/scripts/test_deploy_webhook.py shell out to `git status` and
# scripts/webhooks-handlers-redeploy.py itself runs `git rev-parse --show-toplevel`.
# The host `.git` metadata stays excluded from the Dagger source snapshot, so we
# initialize a throwaway repo at /src — with everything staged and committed —
# to give the script and its tests a valid HEAD to diff against without leaking
# the host's git state.
GIT_INIT_CMD = (
    "git init -q && "
    "git -c user.email=ci@example.com -c user.name=ci "
    "  -c commit.gpgsign=false add -A && "
    "git -c user.email=ci@example.com -c user.name=ci "
    "  -c commit.gpgsign=false commit -q -m 'dagger throwaway' --no-verify"
)


SOURCE_EXCLUDES = [
    ".git",
    ".entire",
    ".kilo",
    ".venv",
    "tmp",
    ".pytest_cache",
    ".ruff_cache",
    "gtm.egg-info",
    "out",
    "data",
    "worktrees",
    # The report this pipeline exports to the host. Left in the repo root by a
    # previous local run, it would feed back into the next run's /src snapshot
    # (fresh timestamps every run) and needlessly invalidate the exec cache.
    "junit.xml",
    "pytest_rc",
]


def build_container() -> dagger.Container:
    """Build the pytest container. Caller must be inside `dagger.connection(...)`."""
    source = dag.host().directory(".", exclude=SOURCE_EXCLUDES)
    uv_cache = dag.cache_volume("uv-cache")
    host_uv_cache = dag.host().directory(str(Path.home() / ".cache" / "uv"))
    # /src/.venv lives on a cache volume, NOT in the container filesystem. A
    # 188-package venv (pyarrow/polars/duckdb) baked into an exec layer forces
    # BuildKit to content-hash a multi-GB diff after pytest — observed in CI as
    # a 7-minute stall on the first result read (the 3-byte pytest_rc fetch
    # paid for the whole snapshot). As a mount the venv never enters a layer,
    # and `uv sync` is an exact reconcile, so a stale volume self-corrects
    # (including across python:3.13 image bumps). GIT_INIT_CMD's `add -A`
    # keeps ignoring it because `.gitignore` covers `.venv/`.
    venv_cache = dag.cache_volume("venv")

    return (
        dag.container()
        .from_("python:3.13")
        .with_exec(
            ["bash", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
        )
        .with_env_variable("PATH", "/root/.local/bin:/usr/local/bin:/usr/bin:/bin")
        # The uv wheel cache and .venv are distinct mounts (separate
        # filesystems), so uv's default hardlink install always fails and
        # falls back to copying with a per-run warning; declare copy mode.
        .with_env_variable("UV_LINK_MODE", "copy")
        .with_mounted_cache(
            "/root/.cache/uv",
            uv_cache,
            source=host_uv_cache,
        )
        .with_directory("/src", source)
        .with_workdir("/src")
        .with_mounted_cache("/src/.venv", venv_cache)
        .with_exec(["bash", "-c", GIT_INIT_CMD])
        # Script tests import the repo-local `scripts` package during
        # collection, so this must install the editable project.
        .with_exec(
            [
                "uv",
                "sync",
                "--all-extras",
                "--dev",
                "--locked",
            ],
        )
        .with_env_variable("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        .with_exec(["bash", "-c", PYTEST_CMD])
    )


async def main() -> None:
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        print("Dagger uv cache: seeding from Namespace host cache (~/.cache/uv)")
        ctr = build_container()

        # Read pytest's real exit code (captured in PYTEST_CMD) first so we know
        # whether a missing report is an expected consequence of a crashed run or
        # a genuine problem. Any failure to read or parse it (missing file from a
        # killed/cancelled container, empty value, non-integer) fails closed at
        # rc=1 so the job goes red with a controlled message, not a traceback.
        try:
            started = perf_counter()
            await ctr.file(PYTEST_RC_PATH).export(PYTEST_RC_HOST_PATH)
            rc = int(Path(PYTEST_RC_HOST_PATH).read_text().strip())
            print(
                "Dagger pipeline evaluation + pytest_rc export: "
                f"{perf_counter() - started:.2f}s",
            )
        except (dagger.DaggerError, OSError, ValueError) as exc:
            sys.stderr.write(
                f"warning: could not read pytest exit code from {PYTEST_RC_PATH} "
                f"({exc}); failing closed at rc=1\n",
            )
            rc = 1

        # Export the report so it reaches the host (and Trunk). A passing run
        # MUST produce one, so an export failure there is fatal (re-raise → red).
        # When pytest already failed, a missing junit.xml is an expected side
        # effect of the crash — warn and keep the real rc rather than masking it.
        try:
            started = perf_counter()
            await ctr.file("/src/junit.xml").export(JUNIT_HOST_PATH)
            print(
                f"Dagger transfer junit.xml: {perf_counter() - started:.2f}s; "
                f"exported report to {JUNIT_HOST_PATH}",
            )
        except dagger.DaggerError as exc:
            if rc == 0:
                raise
            sys.stderr.write(
                f"warning: could not export {JUNIT_HOST_PATH} "
                f"(pytest already exited {rc}): {exc}\n",
            )

    if rc != 0:
        sys.stderr.write(f"pytest exited {rc}\n")
    sys.exit(rc)


if __name__ == "__main__":
    anyio.run(main)
