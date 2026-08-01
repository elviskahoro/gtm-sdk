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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def flox_activate_prefix(repo_root: Path) -> list[str]:
    """The ``flox activate`` argv that wraps each Flox-executor step.

    Takes ``repo_root`` explicitly rather than resolving it from this
    module's own ``__file__`` (e.g. via a shared ``scripts.lib.env.REPO_ROOT``
    constant): the CI pytest pipeline pre-imports ``scripts.lib`` from a
    second, stable checkout at ``/opt/gtm-sdk`` (see
    ``.github/workflows/ci/pytest_dagger.py``'s ``sitecustomize.py`` shim),
    which is a different path than the actual repo under test (``/src``).
    A module-level ``REPO_ROOT`` computed inside ``scripts/lib/`` would
    silently resolve to that stable shim location instead of the caller's
    real checkout. Each caller already computes its own correct
    ``REPO_ROOT`` from its own ``__file__`` and passes it in here.

    ``--mode run`` (not ``dev``): flox refuses a dev-mode activation while
    another shell holds a run-mode one on the same env, and the two modes
    resolve different Nix store paths.
    """
    return ["flox", "activate", "--dir", str(repo_root), "--mode", "run", "--"]
