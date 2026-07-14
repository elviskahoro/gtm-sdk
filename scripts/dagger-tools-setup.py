#!/usr/bin/env -S uv run python
"""Dagger smoke test: install a CLI tool in a container, export it to the host.

`.conductor/settings.toml`'s `setup` script installs `bd`, `roborev`, `dolt`,
and `infisical` via inline `curl | bash` / `dnf install` directly on the host.
This script is a proof-of-concept for doing that installation inside a
Dagger-managed container instead, then exporting just the resulting binary to
the host — so the install step becomes reproducible/cacheable and the host
shell only ever runs a `dagger`-produced artifact, not an arbitrary installer
script fetched at setup time.

Picks `dolt` (the tool with the simplest install: a single upstream
`install.sh` writing one binary to `/usr/local/bin/dolt`) as the
representative case. Exports the binary to
`~/.local/bin/dolt-dagger-test` (not `/usr/local/bin/dolt`) so this smoke test
never collides with or replaces whatever `dolt` the host setup script already
installed. Throwaway experiment — see .conductor/settings.toml for the
wiring.

Usage:
    uv run scripts/dagger_hello_world.py
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import dagger

EXPORT_PATH = Path.home() / ".local" / "bin" / "dolt-dagger-test"


async def main() -> None:
    async with dagger.connection(dagger.Config(log_output=sys.stderr)):
        binary = (
            dagger.dag.container()
            .from_("ubuntu:24.04")
            .with_exec(["bash", "-c", "apt-get update && apt-get install -y --no-install-recommends curl ca-certificates"])
            .with_exec(
                [
                    "bash",
                    "-c",
                    "curl -L https://github.com/dolthub/dolt/releases/latest/download/install.sh | bash",
                ]
            )
            .file("/usr/local/bin/dolt")
        )

        EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        await binary.export(str(EXPORT_PATH))

    EXPORT_PATH.chmod(EXPORT_PATH.stat().st_mode | stat.S_IEXEC)
    os.execv(str(EXPORT_PATH), [str(EXPORT_PATH), "version"])


if __name__ == "__main__":
    asyncio.run(main())
