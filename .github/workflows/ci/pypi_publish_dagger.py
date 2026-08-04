"""Validate, build, and publish both project distributions inside Dagger."""

# ruff: noqa: INP001, ASYNC240 -- workflow script executed directly by Dagger.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import dagger
from dagger import dag

BASE_IMAGE = (
    "ghcr.io/astral-sh/uv:0.11.29-python3.13-trixie-slim"
    "@sha256:0b973c14a35cb0dc8fe63a2e8c9919fd797ac566de13090fcf0df4a6b3994b78"
)


def _release_command() -> list[str]:
    """Return the hermetic validation, build, and publish command."""
    return [
        "bash",
        "-euo",
        "pipefail",
        "-c",
        """
        case "$RELEASE_TAG" in
          v[0-9]*.[0-9]*.[0-9]*|v[0-9]*.[0-9]*.[0-9]*[.-]*) ;;
          *)
            echo "release ref must be a version tag such as v0.1.0: $RELEASE_TAG" >&2
            exit 1
            ;;
        esac

        sdk_version="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
        cli_version="$(python -c 'import tomllib; print(tomllib.load(open("cli/pyproject.toml", "rb"))["project"]["version"])')"
        tag_version="${RELEASE_TAG#v}"
        if [[ "$sdk_version" != "$cli_version" ]]; then
          echo "gtm-sdk version $sdk_version does not match gtm-cli version $cli_version" >&2
          exit 1
        fi
        if [[ "$tag_version" != "$sdk_version" ]]; then
          echo "release tag $RELEASE_TAG does not match package version $sdk_version" >&2
          exit 1
        fi

        rm -rf /dist/gtm-sdk /dist/gtm-cli
        uv build --project /src --out-dir /dist/gtm-sdk
        uv build --project /src/cli --out-dir /dist/gtm-cli
        if [[ "$PUBLISH" == "true" ]]; then
          uv publish --token "$UV_PUBLISH_TOKEN" /dist/gtm-sdk/* /dist/gtm-cli/*
        else
          echo "PUBLISH=false; distributions built but not uploaded"
        fi
        """,
    ]


def _verify_release_checkout(release_tag: str) -> None:
    """Require local publication to use a clean checkout of the release tag."""
    status = subprocess.run(  # noqa: S603, S607 -- fixed git executable and arguments
        ["/usr/bin/git", "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout:
        msg = "PyPI publication requires a clean git worktree"
        raise RuntimeError(msg)

    tag_commit = subprocess.run(  # noqa: S603, S607 -- fixed git executable and arguments
        [
            "/usr/bin/git",
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/tags/{release_tag}^{{}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    head_commit = subprocess.run(  # noqa: S603, S607 -- fixed git executable and arguments
        ["/usr/bin/git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    if (
        tag_commit.returncode != 0
        or tag_commit.stdout.strip() != head_commit.stdout.strip()
    ):
        msg = f"HEAD must resolve to release tag {release_tag} for publication"
        raise RuntimeError(msg)


async def main() -> None:
    """Publish the checked-out release using only Dagger container operations."""
    release_tag = os.environ.get("RELEASE_TAG", "").strip()
    publish = os.environ.get("PUBLISH", "true").strip().lower() == "true"
    token = os.environ.get("PYPI_TOKEN", "").strip()
    if not release_tag:
        msg = "RELEASE_TAG is required, for example v0.1.0"
        raise RuntimeError(msg)
    if publish and not token:
        msg = "PYPI_TOKEN is required for a PyPI publication"
        raise RuntimeError(msg)
    if publish:
        _verify_release_checkout(release_tag)

    source = dag.host().directory(
        str(Path.cwd()),
        exclude=[
            ".git",
            ".env",
            ".env.*",
            ".venv",
            "build",
            "dist",
            "gtm_sdk.egg-info",
            "cli/.venv",
            "cli/build",
            "cli/cli",
            "cli/gtm_cli.egg-info",
            "tmp",
        ],
    )
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        image = (
            dag.container()
            .from_(BASE_IMAGE)
            .with_directory("/src", source)
            .with_workdir("/src")
            .with_env_variable("RELEASE_TAG", release_tag)
            .with_env_variable("PUBLISH", str(publish).lower())
        )
        if publish:
            image = image.with_secret_variable(
                "UV_PUBLISH_TOKEN",
                dag.set_secret("pypi-token", token),
            )
        image = image.with_exec(_release_command())
        await image.sync()


if __name__ == "__main__":
    import anyio

    anyio.run(main)
