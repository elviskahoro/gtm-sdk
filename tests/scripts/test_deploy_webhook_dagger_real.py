"""Real-container regression test for the git-install fix in webhooks-redeploy.

ai-8h3: the Dagger deploy path built from ``DAGGER_BASE_IMAGE``
(``ghcr.io/astral-sh/uv:python3.13-bookworm-slim``) shipped no ``git``, so
``uv sync --frozen`` could not clone the public ``gtm-linear`` git dependency
and aborted with "Git executable not found" before ``modal deploy`` ran. The
fix installs git in the container before the sync.

The mock-based ``test_deploy_webhook_dagger.py`` pins the container call-graph
but cannot know whether the *real* base image actually has git — only running
a real container can. This test does exactly that: it builds from the same
``DAGGER_BASE_IMAGE`` the production deploy uses, runs the same git-install
step, and asserts git is present.

WHY THIS SKIPS IN CI (read before assuming CI covers it): both CI jobs run
pytest *inside* an engine-less ``python:3.13`` Dagger container
(``.github/workflows/ci/pytest_dagger.py`` and
``pytest_integration_dagger.py`` invoke ``dagger run python …``). Opening a
fresh ``dagger.connection()`` from inside an existing Dagger session is the
nested-Dagger case and fails, so ``_can_open_fresh_connection()`` returns
False whenever ``DAGGER_SESSION_PORT`` is set and the test skips. The unit job
additionally runs ``-m 'not integration'``, excluding this file outright.
Net: this test runs only on a developer host with a reachable Docker/Dagger
engine. Its CI value is ~zero — the mock-chain guard in
``test_deploy_webhook_dagger.py`` is the part that runs in CI. This file is
executable proof of the fix and a local pre-merge check.
"""
# trunk-ignore-all(bandit/B607): `docker` resolved via PATH on purpose (engine probe).

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "webhooks-redeploy.py"
_MODULE_NAME = "_webhooks_redeploy_under_test_real"


def _active_docker_unix_socket() -> Path | None:
    """Resolve the active docker context's unix socket, if it points at one.

    ``docker context inspect`` is a local metadata read (no daemon round-trip,
    no network), so it is fast and cannot hang. This catches engines that
    live off the default ``/var/run/docker.sock`` path — Colima
    (``~/.colima/…/docker.sock``), Docker Desktop (``~/.docker/run/…``), etc.
    — which a bare ``/var/run/docker.sock`` stat would miss. Returns None when
    the docker CLI is absent, the endpoint is non-unix (e.g. a TCP host), or
    anything fails to parse.
    """
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        return None
    try:
        endpoint = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    prefix = "unix://"
    if not endpoint.startswith(prefix):
        return None
    sock = Path(endpoint[len(prefix) :])
    return sock if sock.exists() else None


def _can_open_fresh_connection() -> bool:
    """Whether a *new* ``dagger.connection()`` can be opened without nesting.

    Pure env/socket/metadata inspection — never opens a Dagger connection
    (the very thing that hangs or fails in the engine-less CI container), so
    it cannot block test collection.
    """
    # Already inside a `dagger run` session (both CI jobs are): opening a
    # nested connection fails, so skip rather than error.
    if os.environ.get("DAGGER_SESSION_PORT"):
        return False
    if os.environ.get("_EXPERIMENTAL_DAGGER_RUNNER_HOST"):
        return True
    if os.environ.get("DOCKER_HOST"):
        return True
    if Path("/var/run/docker.sock").exists():
        return True
    return _active_docker_unix_socket() is not None


@pytest.fixture(scope="module")
def script_module() -> Iterator[ModuleType]:
    """Load scripts/webhooks-redeploy.py so the test shares DAGGER_BASE_IMAGE.

    The script lives under ``scripts/``, excluded from
    ``[tool.setuptools.packages.find]``, so a normal import doesn't resolve.
    Loading via importlib (mirrors ``test_deploy_webhook_dagger.py``) keeps the
    image constant single-sourced — the test cannot drift from production.
    """
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(_MODULE_NAME, None)


@pytest.mark.asyncio
async def test_git_present_in_base_image_after_install(
    script_module: ModuleType,
) -> None:
    """git is installed in the real deploy image so `uv sync` can clone deps.

    Builds from the production ``DAGGER_BASE_IMAGE`` and runs the same
    git-install step the deploy path uses, then proves git resolves. Catches
    a regression that no mock can: the base image silently dropping git, or
    the install command being wrong for the image's package manager.
    """
    if not _can_open_fresh_connection():
        pytest.skip(
            "no reachable Dagger engine for a fresh connection "
            "(DAGGER_SESSION_PORT set ⇒ nested, or no docker socket); "
            "this real-container test runs only on a host with an engine",
        )

    import dagger

    async with dagger.connection(dagger.Config(log_output=sys.stderr)):
        version = await (
            dagger.dag.container()
            .from_(script_module.DAGGER_BASE_IMAGE)
            .with_exec(
                [
                    "sh",
                    "-c",
                    "apt-get update && apt-get install -y --no-install-recommends git",
                ],
            )
            .with_exec(["git", "--version"])
            .stdout()
        )

    assert version.startswith("git version"), (
        f"git not available after install in {script_module.DAGGER_BASE_IMAGE}; "
        f"got: {version!r}"
    )
