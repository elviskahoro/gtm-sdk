"""Shared Flox invocation helper for this repo's Dagger-fallback scripts.

Dagger cannot run inside Conductor cloud sandboxes (issue #284 -- see
AGENTS.md's "Dagger-fallback pattern (Flox)" section for the root cause).
Every script that offers a Flox fallback for that reason
(``scripts/webhooks-handlers-redeploy.py``, ``scripts/pr-review-threads.py``,
``scripts/hookdeck-connection_events-dump.py``) wraps its host-side command
with :func:`flox_activate_prefix` so all three activate the repo's pinned
Flox environment (``.flox/env/manifest.toml``) identically.
"""

from __future__ import annotations

from scripts.lib.env import REPO_ROOT


def flox_activate_prefix() -> list[str]:
    """The ``flox activate`` argv that wraps each Flox-executor step.

    ``--mode run`` (not ``dev``): flox refuses a dev-mode activation while
    another shell holds a run-mode one on the same env, and the two modes
    resolve different Nix store paths.
    """
    return ["flox", "activate", "--dir", str(REPO_ROOT), "--mode", "run", "--"]
