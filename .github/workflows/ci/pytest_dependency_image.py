"""Build the locked full dependency image used by integration pytest."""

# ruff: noqa: INP001 -- workflow scripts are executed directly by Dagger.

from __future__ import annotations

import dagger
from dagger import dag

SOURCE_EXCLUDES = [
    ".git",
    ".venv",
    "tmp",
    ".pytest_cache",
    ".ruff_cache",
    "gtm.egg-info",
    "out",
    "data",
    "worktrees",
    "junit.xml",
    "pytest_rc",
]
PROJECT_INSTALL_CMD = (
    "uv pip install --no-deps --reinstall --no-build-isolation --offline "
    "--python /opt/venv/bin/python ."
)
FLOX_BUILDER_IMAGE = (
    "ghcr.io/flox/flox@sha256:"
    "c723be35e99ceb5bd6d501e34d32eb6336670e81851f86f316d31512f5ed1a7c"
)


def _flox_toolchain_image(source: dagger.Directory) -> dagger.Container:
    """Materialize the committed Flox toolchain as an ARM64 container."""
    flox = (
        dag.container(platform=dagger.Platform("linux/arm64"))
        .from_(FLOX_BUILDER_IMAGE)
        .with_directory("/workspace/.flox", source.directory(".flox"))
        .with_workdir("/workspace")
        .with_exec(
            [
                "flox",
                "containerize",
                "--dir",
                "/workspace",
                "--file",
                "/workspace/gtm-sdk-flox-toolchain.tar",
            ],
        )
    )
    return dag.container(platform=dagger.Platform("linux/arm64")).import_(
        flox.file("/workspace/gtm-sdk-flox-toolchain.tar"),
    )


def dependency_base(source: dagger.Directory) -> dagger.Container:
    """Build a full, lockfile-checked dependency image for integration tests."""
    context = (
        dag.directory()
        .with_directory(".flox", source.directory(".flox"))
        .with_file("pyproject.toml", source.file("pyproject.toml"))
        .with_file("uv.lock", source.file("uv.lock"))
    )
    return (
        _flox_toolchain_image(source)
        .with_user("root")
        .with_directory("/build", context)
        .with_workdir("/build")
        .with_env_variable("UV_PROJECT_ENVIRONMENT", "/opt/venv")
        .with_env_variable("UV_LINK_MODE", "copy")
        .with_env_variable("HOME", "/home/runner")
        .with_env_variable("UV_CACHE_DIR", "/home/runner/.cache/uv")
        .with_env_variable("PYTHONDONTWRITEBYTECODE", "1")
        .with_exec(
            [
                "bash",
                "-c",
                "uv sync --all-extras --dev --compile-bytecode --locked "
                "--no-install-project --python python3.13",
            ],
        )
        .with_exec(["mkdir", "-p", "/home/runner/.cache/uv"])
        .with_exec(["bash", "-c", "chown -R 1000:1000 /opt/venv /home/runner"])
        .with_user("1000:1000")
    )
