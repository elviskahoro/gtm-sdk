#!/usr/bin/env -S uv run python
# trunk-ignore-all(bandit/B607): list-arg subprocess only; `bd` resolved via PATH on purpose.
"""Copy this repo's beads issues into the Gas Town rig's beads database.

The Gas Town rig (``<town>/gtm_sdk``) is a fresh clone with its own Dolt
beads DB and does NOT share a sync remote with this repo's beads. So a plain
``bd sync`` cannot pull our existing ``ai-*`` tickets into the rig. The
beads-native way to move issues between unrelated DBs is a JSONL round-trip:
``bd export`` here, then ``bd import`` (upsert, preserves IDs + memories)
there.

This is pure local subprocess orchestration — no container env is involved,
so it is plain Python rather than a Dagger script. (Dagger is reserved for
work that needs a reproducible image env, e.g. ``modal deploy``.)

Re-running is safe: ``bd import`` upserts by issue ID, so existing rig copies
are updated in place and the rig's own ``gs-*`` agent/patrol beads are left
untouched.

Usage:
    scripts/beads-sync-to-rig.py                 # export here, import into rig
    scripts/beads-sync-to-rig.py --dry-run       # show counts, change nothing
    scripts/beads-sync-to-rig.py --rig-beads <dir>   # override rig .beads path

The rig location defaults to ``$GT_TOWN_ROOT/gtm_sdk/.beads`` when
``GT_TOWN_ROOT`` is set (Gas Town's shell integration exports it), else
``~/Documents/ai/town/gtm_sdk/.beads``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# This script lives in <repo>/scripts/, so the repo root is its parent's
# parent. Anchor on __file__ — `uv run scripts/...` does NOT chdir, so the
# CWD is wherever the operator invoked the command, not this folder.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

DEFAULT_TOWN_ROOT = Path.home() / "Documents" / "ai" / "town"
DEFAULT_RIG_NAME = "gtm_sdk"


def resolve_source_beads() -> Path | None:
    """Find the source ``.beads`` dir the same way ``bd`` itself resolves it.

    In the primary gtm-sdk checkout, ``.beads`` is a symlink that sits directly
    under the repo root. In a Conductor worktree (the common case) the worktree
    has no local ``.beads`` at all — ``bd`` finds the shared DB by walking up
    the directory tree (e.g. to ``ai/.beads``). Hard-coding ``REPO_ROOT/.beads``
    breaks in every worktree, so we mirror ``bd``'s walk-up here. Starting from
    ``REPO_ROOT`` also covers the symlink case, since ``is_dir()`` follows links.
    """
    for base in (REPO_ROOT, *REPO_ROOT.parents):
        candidate = base / ".beads"
        if candidate.is_dir():
            return candidate.resolve()
    return None


def resolve_rig_beads(override: str | None) -> Path:
    """Locate the rig's .beads dir from --rig-beads, $GT_TOWN_ROOT, or default."""
    if override:
        return Path(override).expanduser().resolve()
    town_root = os.environ.get("GT_TOWN_ROOT")
    base = Path(town_root).expanduser() if town_root else DEFAULT_TOWN_ROOT
    return (base / DEFAULT_RIG_NAME / ".beads").resolve()


def run_bd(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run `bd` in cwd as a list-arg subprocess (never shell=True)."""
    return subprocess.run(  # noqa: S603 — argv list, shell disabled
        ["bd", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rig-beads",
        help="Path to the rig's .beads directory (overrides $GT_TOWN_ROOT / default).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Refresh the export and report what would import, but change nothing.",
    )
    opts = parser.parse_args()

    source_beads = resolve_source_beads()
    if source_beads is None:
        print(
            f"error: no .beads dir found at or above {REPO_ROOT}",
            file=sys.stderr,
        )
        return 1
    source_export = source_beads / "issues.jsonl"
    rig_beads = resolve_rig_beads(opts.rig_beads)

    if not rig_beads.is_dir():
        print(
            f"error: rig beads dir not found: {rig_beads}\n"
            "Is the Gas Town rig created? Try: gtown rig list",
            file=sys.stderr,
        )
        return 1

    # 1. Refresh the source export so issues.jsonl reflects the live DB.
    #    Run `bd export` from the .beads parent so bd targets this exact DB
    #    (its own walk-up would otherwise depend on the invocation CWD).
    print(f"→ exporting beads from {source_beads.parent}")
    run_bd(["export"], cwd=source_beads.parent)
    line_count = sum(1 for _ in source_export.open())
    print(f"  {line_count} record(s) in {source_export}")

    # 2. Import into the rig DB (cwd = rig so bd targets the rig's .beads).
    rig_repo = rig_beads.parent
    import_args = ["import", str(source_export)]
    if opts.dry_run:
        import_args.append("--dry-run")
    print(f"→ {'dry-run import into' if opts.dry_run else 'importing into'} {rig_repo}")
    result = run_bd(import_args, cwd=rig_repo)
    # bd routes the import summary ("Would import N issues") to stderr.
    summary = (result.stdout + result.stderr).strip()
    if summary:
        print(summary)

    if opts.dry_run:
        print("✓ dry run complete — no changes written")
    else:
        print("✓ sync complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
