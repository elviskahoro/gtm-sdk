#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""One-shot git-hook wiring for a fresh clone / new device.

Makes Entire (checkpoint capture) own this repo's git hooks and chains the
project's anti-AI-co-author enforcement (AGENTS.md) behind Entire's hooks.
Idempotent — safe to re-run.

Why this exists: `.git/hooks` is not committed, so a fresh clone has no Entire
hooks, and a new user has none of the maintainer's personal global hooks.
trunk's git-hook actions are disabled in `.trunk/trunk.yaml` so trunk does not
contend for `core.hooksPath` (see AGENTS.md / PR #226).

Prerequisites (the script checks and tells you if either is missing):
  1. Entire CLI installed:  curl -fsSL https://entire.io/install.sh | bash
  2. Logged in:             entire login

Usage:
  scripts/entire-hooks-setup.py          # directly executable (uv shebang)
  uv run scripts/entire-hooks-setup.py   # equivalent
  scripts/entire-hooks-setup.py --force  # replace pre-existing custom hooks
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Anchor on the script's location, never the CWD (see AGENTS.md path anchoring).
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
HOOK_SRC_DIR = SCRIPT_DIR / "git-hooks"

# Hooks whose project logic is chained behind Entire via *.pre-entire backups.
CHAINED_HOOKS = ("prepare-commit-msg", "commit-msg")
# Hooks Entire must install for checkpoints to work (verified at the end).
ENTIRE_HOOKS = ("prepare-commit-msg", "commit-msg", "post-commit", "pre-push")


def _paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def info(msg: str) -> None:
    print(_paint("36", "[setup]"), msg)


def die(msg: str) -> None:
    print(_paint("31", f"[setup] {msg}"), file=sys.stderr)
    raise SystemExit(1)


def run(*args: str, allow_fail: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a command with stdin closed so it can never block on a prompt."""
    proc = subprocess.run(  # noqa: S603 - args are literal, not user input
        args,
        cwd=str(REPO_ROOT),  # cwd-independent: trunk/entire have no -C flag
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0 and not allow_fail:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        die(f"command failed ({proc.returncode}): {' '.join(args)}")
    return proc


def git(*args: str) -> str:
    return run("git", "-C", str(REPO_ROOT), *args).stdout.strip()


def main() -> None:
    force = "--force" in sys.argv[1:]

    # 1. Entire CLI present and authenticated.
    if shutil.which("entire") is None:
        die(
            "Entire CLI not found. Install it, then re-run:\n"
            "    curl -fsSL https://entire.io/install.sh | bash",
        )
    if run("entire", "auth", "status", allow_fail=True).returncode != 0:
        die("Not logged in to Entire. Run `entire login`, then re-run this script.")
    info("Entire CLI present and authenticated.")

    # Resolve the *shared* hooks directory. Use the common git dir (not
    # --absolute-git-dir): core.hooksPath in local config is shared across all
    # linked worktrees, so it must point at the one shared .git/hooks, never a
    # per-worktree private .git/worktrees/<name>/hooks.
    common_dir = Path(git("rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = REPO_ROOT / common_dir
    hooks_dir = (common_dir / "hooks").resolve()
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot the whole hooks dir (name -> content+mode) so any mid-install
    # failure rolls the directory back to its exact prior state, regardless of
    # which files the script or Entire create/overwrite (including any
    # *.pre-entire backups Entire writes for other hooks). A fresh clone falls
    # back to .git/hooks when core.hooksPath is unset, so a stray file left
    # behind would otherwise stay "active".
    snapshot: dict[str, tuple[bytes, int]] = {
        p.name: (p.read_bytes(), p.stat().st_mode)
        for p in hooks_dir.iterdir()
        if p.is_file()
    }

    # Detect pre-existing *custom* hooks we would replace and require an explicit
    # --force opt-in rather than clobbering them silently. Only CHAINED_HOOKS need
    # this: those are the sole hooks this script re-seeds (it unlinks Entire's
    # .pre-entire backup below and rewrites them). Entire's own `entire enable`
    # chains any pre-existing post-commit/pre-push/post-rewrite to <hook>.pre-entire,
    # so those slots are preserved without our help — no guard needed there.
    # (Read-only check; nothing is mutated before this gate.)
    foreign = []
    for name in CHAINED_HOOKS:
        cur = hooks_dir / name
        if not cur.exists():
            continue
        cur_bytes = cur.read_bytes()
        if cur_bytes == (HOOK_SRC_DIR / name).read_bytes():
            continue
        if b"# Entire CLI hooks" in cur_bytes:
            continue
        foreign.append(name)
    if foreign and not force:
        die(
            "Existing custom hook(s) would be replaced: "
            + ", ".join(foreign)
            + ".\n    Re-run with --force to proceed; the originals are saved to "
            "<hook>.local-backup (their logic is NOT re-chained).",
        )

    # 2. Nudge trunk to release hook management if installed. Its git-hook
    #    actions are disabled in .trunk/trunk.yaml, so this is a safety net.
    if shutil.which("trunk") is not None:
        run("trunk", "git-hooks", "sync", allow_fail=True)

    # 3. Point this repo's hooks at .git/hooks so Entire owns them, overriding
    #    any global core.hooksPath the user may already have set. Record the
    #    prior value first so a later failure can be rolled back rather than
    #    leaving the repo pointed at a half-installed hooks dir.
    prev = run(
        "git",
        "-C",
        str(REPO_ROOT),
        "config",
        "--local",
        "--get",
        "core.hooksPath",
        allow_fail=True,
    )
    prior_hookspath = prev.stdout.strip() if prev.returncode == 0 else None
    git("config", "--local", "core.hooksPath", str(hooks_dir))
    info(f"core.hooksPath -> {hooks_dir}")

    try:
        # 3b. --force path: save the custom hooks we're about to replace so their
        #     content isn't lost (a courtesy copy — logic is not re-chained).
        for name in foreign:
            shutil.copyfile(hooks_dir / name, hooks_dir / f"{name}.local-backup")
            info(f"Backed up custom {name} -> {name}.local-backup")

        # 4. Install Entire's checkpoint hooks + the Claude Code agent hooks.
        info("Enabling Entire (agent: claude-code)...")
        run("entire", "enable", "--agent", "claude-code")

        # 5. Chain the anti-co-author hooks behind Entire. Drop any stale
        #    *.pre-entire first (Entire won't clobber an existing backup), seed
        #    our committed hook, and let Entire wrap it (Entire runs first, then
        #    strip/reject). Then re-assert the backup content so the result is
        #    identical no matter what state the clone started in.
        #    shutil.copyfile always overwrites — no `cp -i` alias footgun.
        for name in CHAINED_HOOKS:
            (hooks_dir / f"{name}.pre-entire").unlink(missing_ok=True)
            dst = hooks_dir / name
            shutil.copyfile(HOOK_SRC_DIR / name, dst)
            dst.chmod(0o755)
        info("Seeded anti-co-author hooks; wrapping with Entire...")
        run("entire", "configure", "--force")
        for name in CHAINED_HOOKS:
            backup = hooks_dir / f"{name}.pre-entire"
            shutil.copyfile(HOOK_SRC_DIR / name, backup)
            backup.chmod(0o755)

        # 6. Verify the full chain landed AND is executable — git silently
        #    ignores a hook without the execute bit, so existence isn't enough.
        required = [hooks_dir / h for h in ENTIRE_HOOKS]
        required += [hooks_dir / f"{h}.pre-entire" for h in CHAINED_HOOKS]
        broken = [p.name for p in required if not os.access(p, os.X_OK)]
        if broken:
            die(
                "Setup incomplete; hooks missing or not executable: "
                + ", ".join(broken),
            )
    except BaseException:
        # Roll the hooks dir back to its snapshot (remove created files, restore
        # the rest), then restore core.hooksPath.
        for entry in hooks_dir.iterdir():
            if entry.is_file() and entry.name not in snapshot:
                entry.unlink(missing_ok=True)
        for name, (data, mode) in snapshot.items():
            path = hooks_dir / name
            path.write_bytes(data)
            path.chmod(mode)
        if prior_hookspath is None:
            run(
                "git",
                "-C",
                str(REPO_ROOT),
                "config",
                "--local",
                "--unset",
                "core.hooksPath",
                allow_fail=True,
            )
        else:
            run(
                "git",
                "-C",
                str(REPO_ROOT),
                "config",
                "--local",
                "core.hooksPath",
                prior_hookspath,
                allow_fail=True,
            )
        raise

    info("Done. Entire owns .git/hooks; anti-co-author enforcement is chained.")
    info("Your next commit + push will publish a checkpoint to the Entire console.")


if __name__ == "__main__":
    main()
