"""Shell out to `modal app list --json` and filter to webhook-relevant apps.

Subprocess (not the Modal Python SDK) because that matches the existing
shell-out pattern (see scripts/hookdeck-connection_events-dump.py) and because
no programmatic app-listing API is established in this repo yet.

MODAL_TOKEN_ID / MODAL_TOKEN_SECRET must be set in the environment — inject via
Infisical, never use personal-shell tokens (see CLAUDE.md "Scripted deploy
pitfalls").
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any


def list_deployed_app_names() -> set[str]:
    """Return the set of currently-deployed Modal app names."""
    # Resolve `uv` to its absolute path so bandit doesn't flag B607
    # (partial executable path), and we get a predictable failure if uv
    # isn't on PATH rather than a silent fallthrough.
    uv_path: str | None = shutil.which("uv")
    if uv_path is None:
        msg: str = "uv not found on PATH; cannot run `modal app list`"
        raise RuntimeError(msg)
    result = subprocess.run(  # noqa: S603 - args are fixed, uv_path resolved above
        [uv_path, "run", "modal", "app", "list", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    raw: list[dict[str, Any]] = json.loads(result.stdout)
    return {
        row["Description"]
        for row in raw
        if row.get("State") == "deployed" and row.get("Description")
    }


def modal_url_for_app(app_name: str) -> str:
    """Construct the public Modal endpoint URL for a `@modal.fastapi_endpoint` app.

    Modal converts underscores to hyphens in subdomain slugs. The workspace
    prefix defaults to `devx` (the dlthub team workspace these webhooks deploy
    into); override via MODAL_WORKSPACE if you're targeting a different
    workspace. `modal app list` doesn't expose the workspace name, so this
    can't be derived automatically without a second API call per app.
    """
    workspace_prefix: str = os.environ.get("MODAL_WORKSPACE", "devx")
    slug: str = app_name.replace("_", "-")
    return f"https://{workspace_prefix}--{slug}-web.modal.run"


def warn(msg: str) -> None:
    print(f"[webhook sync] {msg}", file=sys.stderr)
