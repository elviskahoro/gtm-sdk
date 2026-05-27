"""Run every CI check the GitHub workflows run, in one Dagger invocation.

Mirrors the three workflows under `.github/workflows/`:

  - trunk-check.yml         -> `trunk check --all`
  - tests-unit.yml          -> `uv run pytest`
  - tests-integration.yml   -> `uv run pytest -m integration` (needs Infisical creds)

Usage:

    dagger run python scripts/ci.py                       # all jobs
    dagger run python scripts/ci.py --skip integration
    dagger run python scripts/ci.py --only unit

Junit reports are exported to `junit-unit.xml` and `junit-integration.xml` on
the host. Integration tests are skipped (not failed) when `INFISICAL_TOKEN` /
`INFISICAL_PROJECT_ID` are absent. Jobs run concurrently; the script exits
non-zero if any job fails.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

import anyio
import dagger
from dagger import dag

SOURCE_EXCLUDES = [
    ".venv",
    "tmp",
    ".pytest_cache",
    ".ruff_cache",
    "gtm.egg-info",
    "out",
    "data",
    "worktrees",
]

GIT_INIT_CMD = (
    "git init -q && "
    "git -c user.email=ci@example.com -c user.name=ci "
    "  -c commit.gpgsign=false add -A && "
    "git -c user.email=ci@example.com -c user.name=ci "
    "  -c commit.gpgsign=false commit -q -m 'dagger throwaway' --no-verify"
)

UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh"
INFISICAL_INSTALL = (
    "curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | bash && "
    "apt-get update && apt-get install -y infisical"
)
TRUNK_INSTALL = "curl -fsSL https://get.trunk.io | bash -s -- -y"

UNIT_CMD = "uv run pytest --junit-xml=junit.xml -o junit_family=xunit1 || true"
INTEGRATION_CMD = (
    'infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=dev -- '
    "uv run pytest -m integration --junit-xml=junit.xml -o junit_family=xunit1 || true"
)
TRUNK_CMD = "trunk check --all --ci"


@dataclass
class JobResult:
    name: str
    ok: bool
    detail: str = ""


def _source():
    return dag.host().directory(".", exclude=SOURCE_EXCLUDES)


def _python_base(uv_cache):
    return (
        dag.container()
        .from_("python:3.13")
        .with_exec(["bash", "-c", UV_INSTALL])
        .with_env_variable("PATH", "/root/.local/bin:/usr/local/bin:/usr/bin:/bin")
        .with_mounted_cache("/root/.cache/uv", uv_cache)
    )


async def run_unit(results: list[JobResult], uv_cache) -> None:
    try:
        ctr = (
            _python_base(uv_cache)
            .with_directory("/src", _source())
            .with_workdir("/src")
            .with_exec(["bash", "-c", f"rm -rf .git && {GIT_INIT_CMD}"])
            .with_exec(["uv", "sync", "--all-extras", "--dev"])
            .with_exec(["bash", "-c", UNIT_CMD])
        )
        await ctr.file("/src/junit.xml").export("junit-unit.xml")
        results.append(JobResult("unit", ok=True, detail="junit-unit.xml"))
    except Exception as exc:  # noqa: BLE001
        results.append(JobResult("unit", ok=False, detail=str(exc)))


async def run_integration(results: list[JobResult], uv_cache, apt_cache) -> None:
    token = os.environ.get("INFISICAL_TOKEN")
    project_id = os.environ.get("INFISICAL_PROJECT_ID")
    if not token or not project_id:
        results.append(
            JobResult(
                "integration",
                ok=True,
                detail="skipped (INFISICAL_TOKEN/INFISICAL_PROJECT_ID not set)",
            ),
        )
        return
    try:
        ctr = (
            _python_base(uv_cache)
            .with_mounted_cache("/var/cache/apt", apt_cache)
            .with_exec(["bash", "-c", INFISICAL_INSTALL])
            .with_secret_variable(
                "INFISICAL_TOKEN",
                dag.set_secret("infisical-token", token),
            )
            .with_secret_variable(
                "INFISICAL_PROJECT_ID",
                dag.set_secret("infisical-project-id", project_id),
            )
            .with_directory("/src", _source())
            .with_workdir("/src")
            .with_exec(["uv", "sync", "--all-extras", "--dev"])
            .with_exec(["bash", "-c", INTEGRATION_CMD])
        )
        await ctr.file("/src/junit.xml").export("junit-integration.xml")
        results.append(
            JobResult("integration", ok=True, detail="junit-integration.xml"),
        )
    except Exception as exc:  # noqa: BLE001
        results.append(JobResult("integration", ok=False, detail=str(exc)))


async def run_trunk(results: list[JobResult], uv_cache, apt_cache) -> None:
    try:
        ctr = (
            _python_base(uv_cache)
            .with_mounted_cache("/var/cache/apt", apt_cache)
            .with_exec(
                [
                    "bash",
                    "-c",
                    "apt-get update && apt-get install -y git curl ca-certificates",
                ],
            )
            .with_exec(["bash", "-c", TRUNK_INSTALL])
            .with_env_variable(
                "PATH",
                "/root/.trunk/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin",
            )
            .with_directory("/src", _source())
            .with_workdir("/src")
            .with_exec(["bash", "-c", f"rm -rf .git && {GIT_INIT_CMD}"])
            .with_exec(["uv", "sync", "--all-extras", "--dev"])
            .with_env_variable("VIRTUAL_ENV", "/src/.venv")
            .with_env_variable(
                "PATH",
                "/src/.venv/bin:/root/.trunk/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin",
            )
            .with_exec(["bash", "-lc", TRUNK_CMD])
        )
        # Force evaluation
        await ctr.sync()
        results.append(JobResult("trunk", ok=True))
    except Exception as exc:  # noqa: BLE001
        results.append(JobResult("trunk", ok=False, detail=str(exc)))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        choices=["unit", "integration", "trunk"],
        help="Run only one job.",
    )
    parser.add_argument(
        "--skip",
        action="append",
        choices=["unit", "integration", "trunk"],
        default=[],
        help="Skip a job (repeatable).",
    )
    args = parser.parse_args()

    jobs = {"unit", "integration", "trunk"}
    if args.only:
        jobs = {args.only}
    jobs -= set(args.skip)

    results: list[JobResult] = []

    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        uv_cache = dag.cache_volume("uv-cache")
        apt_cache = dag.cache_volume("apt-cache")

        async with anyio.create_task_group() as tg:
            if "unit" in jobs:
                tg.start_soon(run_unit, results, uv_cache)
            if "integration" in jobs:
                tg.start_soon(run_integration, results, uv_cache, apt_cache)
            if "trunk" in jobs:
                tg.start_soon(run_trunk, results, uv_cache, apt_cache)

    print("\n=== CI summary ===")
    for r in results:
        status = "OK  " if r.ok else "FAIL"
        suffix = f" — {r.detail}" if r.detail else ""
        print(f"  [{status}] {r.name}{suffix}")

    if any(not r.ok for r in results):
        sys.exit(1)


if __name__ == "__main__":
    anyio.run(main)
