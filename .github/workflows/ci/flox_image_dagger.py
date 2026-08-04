"""Build the catalog-only Flox image used by isolated script execution.

The image intentionally contains just the committed Flox toolchain: callers
mount their own checkout at runtime, so publishing application source here
would make the image stale on every code change and blur the review boundary.
"""

# ruff: noqa: INP001, ASYNC240, ASYNC221, S603, S607

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import dagger


async def main() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    tar_path = repo_root / "tmp" / "flox-toolchain.tar"
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["flox", "containerize", "--dir", "flox/toolchain", "--file", str(tar_path)],
        cwd=repo_root,
        check=True,
    )
    image_ref = os.environ["FLOX_TOOLCHAIN_IMAGE"]
    token = os.environ["GHCR_TOKEN"]
    registry = image_ref.split("/", maxsplit=1)[0]
    latest_ref = image_ref.rsplit(":", maxsplit=1)[0] + ":latest"
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        image = dagger.dag.container().import_(dagger.dag.host().file(str(tar_path)))
        image = image.with_registry_auth(
            registry,
            "github-actions",
            dagger.dag.set_secret("ghcr-token", token),
        )
        await image.publish(image_ref)
        await image.publish(latest_ref)


if __name__ == "__main__":
    import anyio

    anyio.run(main)
