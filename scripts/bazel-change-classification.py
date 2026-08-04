#!/usr/bin/env -S uv run python
"""Classify a git diff for the required Bazel CI check.

The classifier is intentionally an allowlist.  A path that is not known to be
independent of the Bazel graph or Python runtime requires the full suite.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

SKIPPABLE_EXACT: Final = frozenset(
    {
        ".git-blame-ignore-revs",
        "CHANGELOG.md",
        "LICENSE",
        "README.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
    },
)
SKIPPABLE_PREFIXES: Final = ("docs/", ".github/ISSUE_TEMPLATE/")
BAZEL_CONTROL_NAMES: Final = frozenset(
    {
        ".bazelrc",
        ".bazelversion",
        "BUILD",
        "BUILD.bazel",
        "MODULE.bazel",
        "WORKSPACE",
        "WORKSPACE.bazel",
    },
)


@dataclass(frozen=True)
class Change:
    """One status/path pair from ``git diff --name-status -z``."""

    status: str
    path: str


def parse_changes(raw: bytes) -> list[Change]:
    """Parse NUL-delimited output from ``git diff --name-status -z``."""
    fields = raw.split(b"\0")
    changes: list[Change] = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index].decode("utf-8")
        index += 1
        path_count = 2 if status[:1] in {"R", "C"} else 1
        paths = fields[index : index + path_count]
        if len(paths) != path_count or any(not path for path in paths):
            raise ValueError("malformed git diff --name-status -z output")
        changes.extend(
            Change(status=status, path=path.decode("utf-8")) for path in paths
        )
        index += path_count
    return changes


def _is_skippable_path(path: str) -> bool:
    normalized = PurePosixPath(path).as_posix()
    if PurePosixPath(normalized).name in BAZEL_CONTROL_NAMES or normalized.endswith(
        ".bzl",
    ):
        return False
    return normalized in SKIPPABLE_EXACT or normalized.startswith(SKIPPABLE_PREFIXES)


def classify(changes: list[Change], *, force_full: bool = False) -> tuple[bool, str]:
    """Return ``(run_full, reason)``; uncertainty always requires the suite."""
    if force_full:
        return True, "manual force-full request"
    if not changes:
        return True, "no diff was available"
    for change in changes:
        if change.status[:1] not in {"A", "M"}:
            return True, f"{change.status} change requires conservative validation"
        if not _is_skippable_path(change.path):
            return True, f"{change.path} may affect Bazel targets"
    return False, "all changed paths are documentation or metadata-only"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-full", action="store_true")
    args = parser.parse_args()
    try:
        run_full, reason = classify(
            parse_changes(sys.stdin.buffer.read()),
            force_full=args.force_full,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        print(f"full: malformed diff ({exc})", file=sys.stderr)
        return 2
    mode = "full" if run_full else "skip"
    print(f"{mode}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
