"""Publish the immutable architecture-specific pytest dependency image."""

# ruff: noqa: INP001, ASYNC240 -- workflow script executed directly by Dagger.

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import dagger
from dagger import dag
from pytest_dependency_image import (  # pyrefly: ignore[missing-import]
    build_dependency_image,
    runtime_provenance_command,
)


def _write_github_output(name: str, value: str) -> None:
    """Expose a publication result to a dependent GitHub Actions job."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


async def main() -> None:
    """Build from the published toolchain image and publish the requested tag."""
    source = dag.host().directory(str(Path.cwd()), exclude=[".git", "tmp"])
    toolchain_image = os.environ["FLOX_TOOLCHAIN_IMAGE"]
    image_ref = os.environ["PYTEST_DEPENDENCY_IMAGE"]
    token = os.environ["GHCR_TOKEN"]
    registry = image_ref.split("/", maxsplit=1)[0]
    lock = json.loads(
        (
            Path.cwd() / "flox" / "toolchain" / ".flox" / "env" / "manifest.lock"
        ).read_text(),
    )
    expected_uv_version = lock["manifest"]["install"]["uv"]["version"]

    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        ghcr_secret = dag.set_secret("ghcr-token", token)
        image = build_dependency_image(
            source,
            toolchain_image=toolchain_image,
            registry=registry,
            registry_token=ghcr_secret,
        )
        await image.with_exec(
            runtime_provenance_command(expected_uv_version),
        ).sync()
        published_ref = await image.publish(image_ref)
        _write_github_output("image_ref", published_ref)
        if os.environ.get("FLOX_PUBLISH_LATEST") == "true":
            latest_ref = image_ref.rsplit(":", maxsplit=1)[0] + ":latest"
            await image.publish(latest_ref)


if __name__ == "__main__":
    import anyio

    anyio.run(main)
