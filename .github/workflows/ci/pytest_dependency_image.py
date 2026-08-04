"""Consume the immutable Flox-based dependency image used by pytest."""

# ruff: noqa: INP001 -- workflow scripts are executed directly by Dagger.

from __future__ import annotations

import os
import re

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
PYTEST_DEPENDENCY_IMAGE_ENV = "PYTEST_DEPENDENCY_IMAGE"


def dependency_image_ref() -> str:
    """Return the required immutable image reference from the environment."""
    image = os.environ.get(PYTEST_DEPENDENCY_IMAGE_ENV, "").strip()
    if not image:
        msg = (
            f"{PYTEST_DEPENDENCY_IMAGE_ENV} is required; CI must provide the "
            "architecture-specific immutable pytest dependency image"
        )
        raise RuntimeError(msg)
    tag = image.rsplit(":", maxsplit=1)[-1]
    immutable_tag = re.fullmatch(r"[0-9a-f]{40}-(?:arm64|amd64)", tag)
    if "@sha256:" not in image and immutable_tag is None:
        msg = f"{PYTEST_DEPENDENCY_IMAGE_ENV} must use an immutable tag or digest: {image}"
        raise RuntimeError(msg)
    return image


def dependency_base(source: dagger.Directory) -> dagger.Container:
    """Return the published full-compiled dependency image with the checkout."""
    del source  # The caller mounts the reviewed checkout after this base image.
    return dag.container().from_(dependency_image_ref())


def build_dependency_image(
    source: dagger.Directory,
    *,
    toolchain_image: str,
    registry: str | None = None,
    registry_token: dagger.Secret | None = None,
) -> dagger.Container:
    """Build the publishable full-compiled image from a published toolchain."""
    context = (
        dag.directory()
        .with_file("pyproject.toml", source.file("pyproject.toml"))
        .with_file("uv.lock", source.file("uv.lock"))
    )
    image = dag.container().from_(toolchain_image)
    if registry is not None and registry_token is not None:
        image = image.with_registry_auth(
            registry,
            "github-actions",
            registry_token,
        )
    return (
        image.with_user("root")
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
                (
                    "uv sync --all-extras --dev --compile-bytecode --locked "
                    "--no-install-project --python python3.13"
                ),
            ],
        )
        .with_exec(["mkdir", "-p", "/home/runner/.cache/uv"])
        .with_exec(["bash", "-c", "chown -R 1000:1000 /opt/venv /home/runner"])
        .with_user("1000:1000")
        .with_workdir("/src")
    )


def runtime_provenance_command(expected_uv_version: str) -> list[str]:
    """Return the fail-closed runtime contract for a published image."""
    return [
        "bash",
        "-c",
        " && ".join(
            [
                'test "$(id -u)" = 1000',
                "test -w /opt/venv",
                "test -w /home/runner/.cache/uv",
                f'test "$(uv --version | awk \'{{print $2}}\')" = "{expected_uv_version}"',
                "command -v uv",
                "command -v git",
                "command -v gh",
                "command -v time",
            ],
        ),
    ]
