"""Thin Dagger transport for recipes whose source of truth remains Flox.

The wrapper is an opt-in isolation boundary, not a second toolchain: it starts
from the published Flox image and mounts the caller's reviewed checkout.
"""

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
    """Recognize only wrapper re-entry that reached the trusted Flox image.

    ``CONTAINER_PHASE`` alone is forgeable by a host process. Requiring
    ``FLOX_ENV`` ensures it can suppress recursive wrapping only after the
    image entrypoint has activated its pinned environment.
    """
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
    # Keep Dagger out of module import time: the default Flox path and a
    # wrapper re-entry must work in environments that deliberately lack its SDK.
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
    """Synchronously run one command when the caller owns no event loop.

    Scripts with async orchestration must use the async form instead; nesting
    ``asyncio.run`` would otherwise fail before the container can start.
    """
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
    """Run one command from an existing event loop without nesting one."""
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
    """Run a multi-step recipe whose later commands need earlier filesystem state.

    Use this rather than separate single-command calls when setup (for example
    ``uv sync``) creates files consumed by the following command.
    """
    if command_secrets is not None and len(command_secrets) != len(commands):
        msg = "command_secrets must provide exactly one mapping per command"
        raise ValueError(msg)
    return await _run_in_container(
        repo_root=repo_root,
        commands=commands,
        env=env or {},
        command_secrets=command_secrets or [{} for _ in commands],
        capture=capture,
    )
