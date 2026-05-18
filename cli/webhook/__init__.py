from __future__ import annotations

import json
import sys

import typer

from cli.webhook.registry import (
    REGISTRY_PATH,
    Registry,
    load_registry,
    write_registry,
)
from cli.webhook.sync import build_registry

app = typer.Typer(help="Webhook URL registry (Modal endpoints + Hookdeck wiring).")


@app.command(name="sync")
def sync() -> None:
    """Regenerate webhooks/registry.yaml from live Modal + Hookdeck state."""
    registry: Registry = build_registry()
    write_registry(registry)
    print(
        f"wrote {REGISTRY_PATH.relative_to(REGISTRY_PATH.parents[1])}: "
        f"{len(registry.webhooks)} per-source rows, "
        f"{len(registry.singletons)} singleton apps",
        file=sys.stderr,
    )


@app.command(name="list")
def list_registry() -> None:
    """Print the cached registry as JSON. Errors if no cache; run `sync` first."""
    if not REGISTRY_PATH.exists():
        print(
            f"registry not found at {REGISTRY_PATH}; run `gtm webhook sync` first",
            file=sys.stderr,
        )
        raise typer.Exit(code=1)
    registry: Registry = load_registry()
    print(json.dumps(registry.model_dump(mode="json"), indent=2))
