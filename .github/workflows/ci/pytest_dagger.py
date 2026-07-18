"""Dagger pipeline: run pytest in a container and export the JUnit report.

Invoked the same way locally and in CI. Locally, point at the project's own
`.venv` explicitly — a bare `python` resolves via `$PATH` (e.g. a pyenv
shim), not this repo's `uv`-managed venv where `dagger-io`/`anyio` are
installed:

    dagger run .venv/bin/python .github/workflows/ci/pytest_dagger.py

CI resolves `python` to a dedicated dagger-io/anyio venv via `$GITHUB_PATH`
instead (see `.github/workflows/tests-unit.yml`), so the CI invocation stays
a bare `dagger run python "${pipeline}"`.

The pipeline runs pytest from an immutable, lockfile-derived dependency image
and exports `junit.xml` to the host so a follow-up step (e.g.
trunk-io/analytics-uploader) can upload it. On the ARM64 8x16 Namespace runner,
local measurements favored four explicit xdist workers over both serial and
eight workers: serial 13.16s, four workers 10.74s, eight workers 12.08s.

Trusted main runs publish the image outside this pipeline. Pulling by digest
keeps dependency selection immutable; when no image is available, Dagger builds
the same dependency-only Dockerfile locally without publishing it.

The pipeline *fails* (non-zero exit) when pytest exits non-zero, while still
exporting the report. A previous `... || true` swallowed pytest's exit code so
the job went green even on a failing suite (ai-eun); we instead capture pytest's
exit code into `/src/pytest_rc` (the trailing `echo` keeps the `with_exec` green
so the report stays exportable) and re-raise it after the export.
"""

from __future__ import annotations

import os
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
    "printf '%s\\n' 'import sys; sys.path.insert(0, \"/opt/gtm-sdk\"); import scripts.lib' "
    ">/tmp/sitecustomize.py; "
    "export PYTHONPATH=/tmp:/opt/gtm-sdk:/src${PYTHONPATH:+:$PYTHONPATH}; "
    '"/opt/venv/bin/python" -m pytest '
    "-p xdist.plugin -p pytest_asyncio.plugin -p anyio.pytest_plugin "
    "-n 4 --dist=loadfile "
    "--junit-xml=junit.xml -o junit_family=xunit1; "
    "echo $? > /src/pytest_rc"
)
DEPENDENCY_CHECK_CMD = "uv sync --all-extras --dev --locked --no-install-project --inexact --check --python /opt/venv/bin/python"
PROJECT_INSTALL_CMD = (
    "uv pip install --no-deps --reinstall --no-build-isolation --offline "
    "--python /opt/venv/bin/python ."
)
JUNIT_HOST_PATH = "junit.xml"
PYTEST_RC_PATH = "/src/pytest_rc"
PYTEST_RC_HOST_PATH = "pytest_rc"
DEPENDENCY_DOCKERFILE_PATH = ".github/workflows/ci/pytest-deps.Dockerfile"

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


def dependency_build_context(source: dagger.Directory) -> dagger.Directory:
    dockerfile_override = os.environ.get(
        "PYTEST_DEPENDENCY_DOCKERFILE",
        "",
    ).strip()
    if dockerfile_override:
        dockerfile = dag.host().file(dockerfile_override)
    else:
        dockerfile = source.file(DEPENDENCY_DOCKERFILE_PATH)

    return (
        dag.directory()
        .with_file("pyproject.toml", source.file("pyproject.toml"))
        .with_file("uv.lock", source.file("uv.lock"))
        .with_file("pytest-deps.Dockerfile", dockerfile)
    )


def dependency_base(source: dagger.Directory) -> dagger.Container:
    """Return the immutable dependency image or build it locally when absent."""
    dependency_image = os.environ.get("PYTEST_DEPENDENCY_IMAGE", "").strip()
    if not dependency_image:
        print("Pytest dependency image: unavailable; building cold")
        return dependency_build_context(source).docker_build(
            dockerfile="pytest-deps.Dockerfile",
            platform=dagger.Platform("linux/arm64"),
        )

    if "@sha256:" not in dependency_image:
        raise ValueError(
            "PYTEST_DEPENDENCY_IMAGE must be an immutable digest reference",
        )

    print(f"Pytest dependency image: {dependency_image}")
    registry_host = dependency_image.split("/", 1)[0]
    container = dag.container(platform=dagger.Platform("linux/arm64"))
    registry_token = os.environ.get("NAMESPACE_REGISTRY_TOKEN", "").strip()
    if registry_token:
        registry_secret = dag.set_secret("namespace-registry-token", registry_token)
        return (
            container.with_registry_auth(registry_host, "token", registry_secret)
            .from_(dependency_image)
            .without_registry_auth(registry_host)
        )

    return container.from_(dependency_image)


def build_container() -> dagger.Container:
    """Build the pytest container. Caller must be inside `dagger.connection(...)`."""
    source = dag.host().directory(".", exclude=SOURCE_EXCLUDES)
    scripts = dag.host().directory("scripts")
    base = dependency_base(source)

    return (
        base.with_directory("/src", source, owner="runner")
        # Keep the small repo-local helper package under a separate import
        # root so an incomplete `/src` snapshot cannot shadow `scripts.lib`.
        .with_directory("/opt/gtm-sdk/scripts", scripts, owner="runner")
        .with_workdir("/src")
        .with_env_variable("PYTHONPATH", "/src")
        .with_env_variable("PYTHONDONTWRITEBYTECODE", "1")
        .with_exec(["bash", "-c", GIT_INIT_CMD])
        .with_env_variable("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        .with_exec(["bash", "-c", DEPENDENCY_CHECK_CMD])
        .with_exec(["bash", "-c", PROJECT_INSTALL_CMD])
        .with_exec(["bash", "-c", PYTEST_CMD])
    )


async def main() -> None:
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
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
