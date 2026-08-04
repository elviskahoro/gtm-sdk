"""Thin Dagger transport for scripts whose recipe lives in Flox."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

CONTAINER_PHASE = "CONTAINER_PHASE"
RUN_WITH_DAGGER = "RUN_WITH_DAGGER"
CONTAINER_IMAGE = "CONTAINER_IMAGE"
DEFAULT_CONTAINER_IMAGE = "ghcr.io/elviskahoro/gtm-sdk/flox-toolchain:latest"

SOURCE_EXCLUDES = [
    ".git",
    ".git/",
    ".venv/",
    "tmp/",
    "**/__pycache__/",
    "*.pyc",
]


def in_container_phase() -> bool:
    """Return true only for a wrapper-launched process with Flox activated."""
    if not os.environ.get(CONTAINER_PHASE):
        return False
    if not os.environ.get("FLOX_ENV"):
        msg = "CONTAINER_PHASE requires an activated Flox environment"
        raise RuntimeError(msg)
    return True


def _secret_name(base: str, value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"{base.lower().replace('_', '-')}-{digest}"


async def _run_in_container(
    *,
    repo_root: Path,
    commands: Sequence[Sequence[str]],
    env: Mapping[str, str],
    command_secrets: Sequence[Mapping[str, str]],
    capture: bool,
) -> str | None:
    # Import lazily so the Flox-primary path and the container phase only need
    # Python from the toolchain image, not the Dagger SDK in their environment.
    import dagger

    image = os.environ.get(CONTAINER_IMAGE, DEFAULT_CONTAINER_IMAGE).strip()
    async with dagger.connection(dagger.Config(log_output=sys.stderr)):
        source = dagger.dag.host().directory(str(repo_root), exclude=SOURCE_EXCLUDES)
        container = (
            dagger.dag.container()
            .from_(image)
            .with_directory("/repo", source)
            .with_workdir("/repo")
            .with_env_variable(CONTAINER_PHASE, "1")
        )
        for name, value in env.items():
            container = container.with_env_variable(name, value)
        step = container
        for index, command in enumerate(commands):
            for name, value in command_secrets[index].items():
                step = step.with_secret_variable(
                    name,
                    dagger.dag.set_secret(_secret_name(name, value), value),
                )
            step = step.with_exec(list(command), use_entrypoint=True)
        if capture:
            return await step.stdout()
        await step.sync()
        return None


def run_in_container(
    *,
    repo_root: Path,
    argv: Sequence[str],
    env: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
    capture: bool = False,
) -> str | None:
    """Re-execute ``argv`` inside the prebuilt Flox image."""
    return asyncio.run(
        run_in_container_async(
            repo_root=repo_root,
            argv=argv,
            env=env or {},
            secrets=secrets,
            capture=capture,
        ),
    )


async def run_in_container_async(
    *,
    repo_root: Path,
    argv: Sequence[str],
    env: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
    capture: bool = False,
) -> str | None:
    """Async form for scripts whose recipe already runs in an event loop."""
    return await run_recipe_in_container_async(
        repo_root=repo_root,
        commands=[argv],
        env=env,
        command_secrets=[secrets or {}],
        capture=capture,
    )


async def run_recipe_in_container_async(
    *,
    repo_root: Path,
    commands: Sequence[Sequence[str]],
    env: Mapping[str, str] | None = None,
    command_secrets: Sequence[Mapping[str, str]] | None = None,
    capture: bool = False,
) -> str | None:
    """Run several commands in one container filesystem and Flox shell."""
    return await _run_in_container(
        repo_root=repo_root,
        commands=commands,
        env=env or {},
        command_secrets=command_secrets or [{} for _ in commands],
        capture=capture,
    )
