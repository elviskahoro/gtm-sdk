#!/usr/bin/env -S uv run python
# trunk-ignore-all(bandit/B607): list-arg subprocess only; `bd` resolved via PATH on purpose.
"""Copy this repo's beads issues into the Gas Town rig's beads database.

The Gas Town rig (``<town>/gtm_sdk``) is a fresh clone with its own Dolt
beads DB and does NOT share a sync remote with this repo's beads. So a plain
``bd sync`` cannot pull our existing ``gtm-*`` tickets into the rig, and
without them a polecat working in the rig sees only the rig's own ``gs-*``
agent/patrol beads — none of the actual backlog. The beads-native way to move
issues between unrelated DBs is a JSONL round-trip: ``bd export`` here, then
``bd import`` (upsert, preserves IDs + memories) there.

This is pure local subprocess orchestration — no container env is involved,
so it is plain Python rather than a Dagger script. (Dagger is reserved for
work that needs a reproducible image env, e.g. ``modal deploy``.)

Re-running is safe: ``bd import`` upserts by issue ID, so existing rig copies
are updated in place and the rig's own ``gs-*`` agent/patrol beads are left
untouched.

The rig's ``.beads/`` is deliberately gitignored in the Gas Town town repo
(Gas Town tracks beads via Dolt's ``refs/dolt/data``, not the JSONL export).
bd's post-write auto-export therefore can't ``git add .beads/issues.jsonl`` and
prints a scary-but-benign ``auto-export: git add failed`` warning on every real
import. To keep that noise from being mistaken for a sync failure, we disable
``export.git-add`` on the rig (see ``ensure_rig_export_git_add_disabled``). That
flips only the git-staging step — auto-export still refreshes ``issues.jsonl``
and the Dolt commit we rely on is untouched (unlike ``--sandbox``, which would
disable auto-sync).

``bd export`` writes JSONL to **stdout**, so the intermediate file is passed
explicitly with ``-o``. Do not "simplify" that back to a bare ``bd export`` on
the assumption it refreshes ``.beads/issues.jsonl``: this repo sets
``export.auto: false``, so nothing else writes that file and the import step
would read a stale copy — or, as happened here, crash on a file that never
existed. The export lands in ``tmp/`` (gitignored scratch, per AGENTS.md)
rather than in ``.beads/``, so a sync never mutates the source DB's own
passive export. ``--include-memories`` is required too — ``bd export`` now
excludes memories by default, and ``bd import`` re-materializes any it finds
as ``bd remember`` entries.

Usage:
    scripts/beads-rig-sync_to.py                 # export here, import into rig
    scripts/beads-rig-sync_to.py --dry-run       # show counts, change nothing
    scripts/beads-rig-sync_to.py --rig-beads <dir>   # override rig .beads path
    scripts/beads-rig-sync_to.py --source-beads <dir>   # override source .beads path

The rig location comes from ``$GT_TOWN_ROOT/gtm_sdk/.beads`` when
``GT_TOWN_ROOT`` is set (Gas Town's shell integration exports it — but note
that ``GASTOWN_DISABLED=1`` suppresses the hook, so an interactive shell
usually does NOT have it). Otherwise the first existing town in
``TOWN_ROOT_CANDIDATES`` wins. List the rigs a town actually has with
``gastown rig list`` from inside the town directory.
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

# Towns to probe when $GT_TOWN_ROOT is unset, most-current first. The Gas Town
# workspace moved out of `ai/` — keep the old path as a fallback so an operator
# on an older layout still resolves instead of erroring on a path that never
# existed on their machine.
TOWN_ROOT_CANDIDATES = (
    Path.home() / "Documents" / "town",
    Path.home() / "Documents" / "ai" / "town",
)
DEFAULT_RIG_NAME = "gtm_sdk"

# Scratch destination for the intermediate JSONL. `tmp/` is gitignored (AGENTS.md
# reserves it for exactly this); writing into `.beads/` would clobber the source
# DB's passive export.
EXPORT_PATH = REPO_ROOT / "tmp" / "beads-rig-sync-export.jsonl"


def resolve_source_beads(override: str | None) -> Path | None:
    """Find the source ``.beads`` dir the same way ``bd`` itself resolves it.

    In the primary gtm-sdk checkout, ``.beads`` is a symlink that sits directly
    under the repo root. In a Conductor worktree (the common case) the worktree
    has no local ``.beads`` at all — ``bd`` finds the shared DB by walking up
    the directory tree (e.g. to ``ai/.beads``). Hard-coding ``REPO_ROOT/.beads``
    breaks in every worktree, so we mirror ``bd``'s walk-up here. Starting from
    ``REPO_ROOT`` also covers the symlink case, since ``is_dir()`` follows links.

    An explicit ``--source-beads`` is honored verbatim, even if it does not
    exist, mirroring ``resolve_rig_beads``'s override contract — this is also
    what lets tests point the script at a synthetic directory instead of the
    real (gitignored) ``.beads`` walk-up, which doesn't exist at all in a
    fresh CI checkout.
    """
    if override:
        return Path(override).expanduser().resolve()
    for base in (REPO_ROOT, *REPO_ROOT.parents):
        candidate = base / ".beads"
        if candidate.is_dir():
            return candidate.resolve()
    return None


def resolve_rig_beads(override: str | None) -> Path:
    """Locate the rig's .beads dir from --rig-beads, $GT_TOWN_ROOT, or a candidate town.

    An explicit ``--rig-beads`` or ``$GT_TOWN_ROOT`` is honored verbatim, even if
    it does not exist — an operator who names a path deserves an error naming
    that same path. Only the candidate scan probes the filesystem, returning the
    first town that actually holds the rig and falling back to the preferred
    candidate so the not-found message points at the modern layout.
    """
    if override:
        return Path(override).expanduser().resolve()
    town_root = os.environ.get("GT_TOWN_ROOT")
    if town_root:
        return (Path(town_root).expanduser() / DEFAULT_RIG_NAME / ".beads").resolve()
    for candidate in TOWN_ROOT_CANDIDATES:
        rig_beads = candidate / DEFAULT_RIG_NAME / ".beads"
        if rig_beads.is_dir():
            return rig_beads.resolve()
    return (TOWN_ROOT_CANDIDATES[0] / DEFAULT_RIG_NAME / ".beads").resolve()


class BdCommandError(RuntimeError):
    """A ``bd`` invocation failed. Carries bd's own output for the operator."""

    def __init__(
        self,
        args: list[str],
        result: subprocess.CompletedProcess[str],
    ) -> None:
        self.args_run = args
        self.result = result
        super().__init__(f"bd {' '.join(args)} exited {result.returncode}")


def run_bd(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run `bd` in cwd as a list-arg subprocess (never shell=True).

    Raises ``BdCommandError`` rather than ``CalledProcessError`` on failure.
    Both stdout and stderr are captured, so a bare ``check=True`` would render
    the one thing the operator needs — bd's diagnostic, which is often several
    lines of "common causes" — as an unprintable attribute on a traceback. Every
    real failure here (Dolt server down, database missing, unreadable JSONL) is
    diagnosable *only* from that text, so the caller re-prints it verbatim.
    """
    result = subprocess.run(  # noqa: S603 — argv list, shell disabled
        ["bd", *args],
        cwd=cwd,
        check=False,  # handled below so bd's own message survives
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise BdCommandError(args, result)
    return result


def ensure_rig_export_git_add_disabled(rig_repo: Path) -> None:
    """Turn off bd auto-export's ``git add`` on the rig (idempotent).

    See the module docstring for why: the rig's gitignored ``.beads/`` makes
    every real import emit a benign ``auto-export: git add failed`` warning.
    Setting ``export.git-add false`` silences it without touching the Dolt
    commit. We gate on the current value so steady-state runs stay write-free —
    the one-time ``config set`` only fires on a freshly cloned rig (and its own
    warning is captured, not printed).
    """
    current = subprocess.run(  # noqa: S603 — argv list, shell disabled
        ["bd", "config", "get", "export.git-add"],
        cwd=rig_repo,
        check=False,  # unset key may exit non-zero; treat as "needs setting"
        text=True,
        capture_output=True,
    )
    if current.stdout.strip() == "false":
        return
    run_bd(["config", "set", "export.git-add", "false"], cwd=rig_repo)


def report_bd_failure(exc: BdCommandError) -> None:
    """Print bd's own diagnostic for a failed invocation.

    A rig whose Dolt server is stopped, or whose ``gtm_sdk`` database is missing
    from the data directory the server is actually serving, fails here — and bd
    prints the recovery steps (``bd dolt start``, ``bd doctor``, ``bd bootstrap``)
    that this script deliberately does not run on the operator's behalf: they
    mutate another workspace's runtime state.
    """
    detail = (exc.result.stderr + exc.result.stdout).strip()
    print(f"error: bd {' '.join(exc.args_run)} failed", file=sys.stderr)
    if detail:
        print(detail, file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rig-beads",
        help="Path to the rig's .beads directory (overrides $GT_TOWN_ROOT / default).",
    )
    parser.add_argument(
        "--source-beads",
        help="Path to this repo's .beads directory (overrides the walk-up default).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Refresh the export and report what would import, but change nothing.",
    )
    opts = parser.parse_args()

    try:
        return run_sync(opts)
    except BdCommandError as exc:
        report_bd_failure(exc)
        return 1


def run_sync(opts: argparse.Namespace) -> int:
    """Export here, import into the rig. Raises BdCommandError if bd fails."""
    source_beads = resolve_source_beads(opts.source_beads)
    if source_beads is None:
        print(
            f"error: no .beads dir found at or above {REPO_ROOT}",
            file=sys.stderr,
        )
        return 1
    source_export = EXPORT_PATH
    rig_beads = resolve_rig_beads(opts.rig_beads)

    if not rig_beads.is_dir():
        print(
            f"error: rig beads dir not found: {rig_beads}\n"
            "Is the Gas Town rig created? From the town dir, try: gastown rig list",
            file=sys.stderr,
        )
        return 1

    # 1. Dump the live DB to the scratch export.
    #    Run `bd export` from the .beads parent so bd targets this exact DB
    #    (its own walk-up would otherwise depend on the invocation CWD), and
    #    pass an absolute -o so the output lands in tmp/ regardless of that cwd.
    source_export.parent.mkdir(parents=True, exist_ok=True)
    print(f"→ exporting beads from {source_beads.parent}")
    run_bd(
        ["export", "--include-memories", "-o", str(source_export)],
        cwd=source_beads.parent,
    )
    line_count = sum(1 for _ in source_export.open())
    print(f"  {line_count} record(s) in {source_export}")

    # 2. Import into the rig DB (cwd = rig so bd targets the rig's .beads).
    rig_repo = rig_beads.parent
    # A real import writes, which triggers the rig's auto-export git-add warning;
    # disable it first. --dry-run writes nothing, so it never warns — skip the
    # config write there to keep the dry run truly read-only.
    if not opts.dry_run:
        ensure_rig_export_git_add_disabled(rig_repo)
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
