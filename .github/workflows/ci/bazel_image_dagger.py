"""Provide a stable ARM64 runtime so Bazel CI avoids repeated provisioning."""

# ruff: noqa: INP001, ASYNC240 -- workflow script executed directly by Dagger.

from __future__ import annotations

import os
import sys

import dagger
from dagger import dag

BASE_IMAGE = (
    "ghcr.io/astral-sh/uv:0.11.29-python3.13-trixie-slim"
    "@sha256:0b973c14a35cb0dc8fe63a2e8c9919fd797ac566de13090fcf0df4a6b3994b78"
)


async def main() -> None:
    """Publish the runtime image that removes per-run Bazel CI setup work."""
    image_ref = os.environ["BAZEL_CI_IMAGE"]
    username = os.environ.get("GHCR_USERNAME", "github-actions")
    token = os.environ["GHCR_TOKEN"]
    registry = image_ref.split("/", maxsplit=1)[0]
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        image = (
            dag.container(platform=dagger.Platform("linux/arm64"))
            .from_(BASE_IMAGE)
            .with_exec(
                [
                    "bash",
                    "-c",
                    "apt-get update && apt-get install --yes --no-install-recommends "
                    "build-essential ca-certificates curl git unzip && "
                    "rm -rf /var/lib/apt/lists/* && "
                    "ln -sf /usr/local/bin/python3.13 /usr/local/bin/python3 && "
                    "ln -sf /usr/local/bin/python3.13 /usr/local/bin/python",
                ],
            )
            .with_registry_auth(
                registry,
                username,
                dag.set_secret("ghcr-token", token),
            )
        )
        await image.publish(image_ref)


if __name__ == "__main__":
    import anyio

    anyio.run(main)
