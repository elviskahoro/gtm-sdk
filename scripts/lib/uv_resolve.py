"""Resolve a `uv` binary that satisfies this repo's `[tool.uv] required-version`.

Stdlib-only, deliberately: `webhooks-handlers-redeploy.py`'s shebang bootstrap
imports this before any uv-managed venv is guaranteed active (it may run under
a bare ambient `python3`), and `conductor-workspace-setup.sh` / `.kilo/setup-script`
invoke it directly via `python3 scripts/lib/uv_resolve.py`. No third-party
imports -- not even `packaging` or `tomllib` (3.11+ only) -- for that reason.
"""

from __future__ import annotations

import operator
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable

SCRIPT_DIR = Path(__file__).resolve().parents[1]


def _find_repo_root() -> Path:
    """Find the project root from the helper path or its invocation directory.

    CI mounts this helper under ``/opt/gtm-sdk/scripts`` while the checkout is
    mounted at ``/src``. Prefer the helper's normal location, then search the
    current working directory so that copied helper mounts still use the
    checkout's ``pyproject.toml`` and probe ``uv`` from the checkout root.
    """
    search_starts = (SCRIPT_DIR.parent, Path.cwd())
    seen: set[Path] = set()
    for start in search_starts:
        for ancestor in (start, *start.parents):
            candidate = ancestor.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            if (candidate / "pyproject.toml").is_file():
                return candidate
    msg = "could not locate pyproject.toml from the uv resolver or its cwd"
    raise FileNotFoundError(msg)


REPO_ROOT = _find_repo_root()
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

DEFAULT_FALLBACK_LOCATIONS: tuple[str, ...] = (
    # $HOME-relative only, deliberately -- these are the locations a
    # just-installed uv could land in before PATH is updated to include it
    # (e.g. right after the curl installer runs). Hardcoded absolute paths
    # like /opt/homebrew/bin were deliberately left out: Homebrew's own
    # install process already puts that dir on PATH, so it's always found
    # by the PATH scan above anyway, and baking in a fixed absolute path
    # would silently leak real machine state into anything that sandboxes
    # PATH but not the filesystem (e.g. tests).
    str(Path.home() / ".local/bin/uv"),
    str(Path.home() / ".cargo/bin/uv"),
)

_REQUIRED_VERSION_RE = re.compile(r'required-version\s*=\s*"([^"]+)"')
_VERSION_OUTPUT_RE = re.compile(r"uv (\d+(?:\.\d+){0,2})")
_CLAUSE_RE = re.compile(r"(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+){0,3})")


class UvCandidate(NamedTuple):
    path: str
    version: tuple[int, ...] | None  # None => --version failed or was unparseable
    raw_output: str


def extract_required_version(text: str) -> str:
    """Pull `required-version = "..."` out of a pyproject.toml's `[tool.uv]` table."""
    match = _REQUIRED_VERSION_RE.search(text)
    if match is None:
        msg = "no [tool.uv] required-version found in pyproject.toml"
        raise ValueError(msg)
    return match.group(1)


def _parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def parse_range(required_range: str) -> list[tuple[str, tuple[int, ...]]]:
    """Parse a comma-joined `>=X.Y.Z,<A.B.C`-style range into (op, version) clauses.

    Only the simple comparator form actually used by `required-version` --
    no need for a general PEP 440 implementation. Raises ValueError on a
    clause it doesn't recognize; never silently treats one as satisfied.
    """
    clauses: list[tuple[str, tuple[int, ...]]] = []
    for raw_clause in required_range.split(","):
        clause = raw_clause.strip()
        match = _CLAUSE_RE.fullmatch(clause)
        if match is None:
            msg = f"unrecognized version clause: {clause!r}"
            raise ValueError(msg)
        op, version_text = match.groups()
        clauses.append((op, _parse_version(version_text)))
    return clauses


_COMPARATORS: dict[str, Callable[[tuple[int, ...], tuple[int, ...]], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}


def version_satisfies(
    version: tuple[int, ...],
    clauses: list[tuple[str, tuple[int, ...]]],
) -> bool:
    """Check whether `version` satisfies every (op, bound) clause.

    Both tuples are right-padded to equal length with trailing zeros before
    comparing -- raw tuple comparison makes ``(0, 12) < (0, 12, 0)`` (a
    shorter-but-equal-prefix tuple sorts first), which would incorrectly
    make ``"0.12.0"`` satisfy ``<0.12``.
    """
    for op, bound in clauses:
        width = max(len(version), len(bound))
        v = version + (0,) * (width - len(version))
        b = bound + (0,) * (width - len(bound))
        if not _COMPARATORS[op](v, b):
            return False
    return True


def iter_candidate_paths(
    *,
    cwd: str,
    path_env: str | None = None,
    fallback_locations: tuple[str, ...] = DEFAULT_FALLBACK_LOCATIONS,
) -> list[str]:
    """Every executable `uv` on PATH (in order), then well-known fallback locations.

    Deliberately not `shutil.which` -- that returns only the first match,
    which is exactly the bug this module exists to route around.

    `cwd` is required (no ambient-cwd default) and must be the same
    directory the caller will later pass to `probe_uv`: a relative PATH
    entry -- or a conventional empty segment, meaning "." -- has to be
    resolved against *some* directory, and if that differs from the probe's
    `cwd`, the existence check here and the actual `--version` invocation
    later could disagree about what the candidate even is. Every returned
    candidate is normalized to an absolute path so that ambiguity can't
    reappear downstream. Dedupes by realpath so a symlinked alias doesn't
    get probed twice.
    """
    base = Path(cwd)
    raw_path = path_env if path_env is not None else os.environ.get("PATH", "")
    candidates: list[str] = []
    seen_realpaths: set[str] = set()

    def _consider(candidate: str) -> None:
        if not candidate:
            return
        candidate_path = Path(candidate)
        if not candidate_path.is_absolute():
            candidate_path = base / candidate_path
        if not candidate_path.is_file() or not os.access(candidate_path, os.X_OK):
            return
        real = str(candidate_path.resolve())
        if real in seen_realpaths:
            return
        seen_realpaths.add(real)
        candidates.append(str(candidate_path))

    for raw_directory in raw_path.split(os.pathsep):
        directory = raw_directory or "."  # empty PATH segment conventionally means cwd
        _consider(str(Path(directory) / "uv"))
    for fallback in fallback_locations:
        _consider(fallback)
    return candidates


def probe_uv(path: str, *, cwd: str) -> UvCandidate:
    """Run `<path> --version`, always from `cwd`.

    A pyenv shim can resolve to a *different* real binary depending on which
    directory's Python version is active, so probing from the wrong cwd can
    report a version that has nothing to do with what would actually run for
    a real invocation -- always pass the actual repo/invocation directory.

    Any failure (nonzero exit, timeout, unparseable output) yields
    ``version=None`` rather than raising; one broken candidate never aborts
    the scan. `uv --version` itself is exempt from the `required-version`
    self-check gate (confirmed empirically), so probing every candidate this
    way is always safe.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, shell disabled, path is filesystem-discovered
            [path, "--version"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return UvCandidate(path=path, version=None, raw_output=str(exc))
    raw_output = (proc.stdout or "") + (proc.stderr or "")
    match = _VERSION_OUTPUT_RE.search(raw_output)
    if proc.returncode != 0 or match is None:
        return UvCandidate(path=path, version=None, raw_output=raw_output.strip())
    return UvCandidate(
        path=path,
        version=_parse_version(match.group(1)),
        raw_output=raw_output.strip(),
    )


class NoCompatibleUvError(RuntimeError):
    """Raised when no candidate `uv` satisfies the required version range."""

    def __init__(self, tried: list[UvCandidate], required_range: str) -> None:
        """Store `tried` candidates and bake `format_remediation(...)` into the message."""
        self.tried = tried
        self.required_range = required_range
        super().__init__(format_remediation(tried, required_range))


def find_compatible_uv(
    required_range: str,
    *,
    cwd: str,
    path_env: str | None = None,
    fallback_locations: tuple[str, ...] = DEFAULT_FALLBACK_LOCATIONS,
) -> UvCandidate:
    """Return the first `uv` on PATH (in order) satisfying `required_range`.

    Raises `NoCompatibleUvError` (carrying every candidate tried) if none do.
    Taking `required_range`/`path_env` as plain parameters, rather than
    reading them internally, is what makes this directly unit-testable with
    fabricated PATHs and versions.
    """
    clauses = parse_range(required_range)
    tried: list[UvCandidate] = []
    for path in iter_candidate_paths(
        cwd=cwd,
        path_env=path_env,
        fallback_locations=fallback_locations,
    ):
        candidate = probe_uv(path, cwd=cwd)
        tried.append(candidate)
        if candidate.version is not None and version_satisfies(
            candidate.version,
            clauses,
        ):
            return candidate
    raise NoCompatibleUvError(tried, required_range)


def find_compatible_uv_for_repo(*, cwd: str | None = None) -> UvCandidate:
    """Convenience wrapper real callers use: resolve against this repo's own range."""
    required_range = extract_required_version(PYPROJECT_PATH.read_text())
    return find_compatible_uv(required_range, cwd=cwd or str(REPO_ROOT))


def format_remediation(tried: list[UvCandidate], required_range: str) -> str:
    """Render what was checked, the required range, and a concrete fix.

    One implementation, shared by `NoCompatibleUvError.__str__`, the CLI's
    stderr output, and the in-process preflight's `_fail()` call, so wording
    never drifts between call sites.
    """
    lines = [
        f"No installed `uv` satisfies the required version range {required_range}.",
    ]
    if tried:
        lines.append("Checked:")
        for candidate in tried:
            if candidate.version is not None:
                version_text = ".".join(map(str, candidate.version))
            else:
                version_text = f"unparseable ({candidate.raw_output[:80]!r})"
            lines.append(f"  {candidate.path} -> {version_text}")
    else:
        lines.append("No `uv` binary was found on PATH or in common install locations.")
    lines.append(
        "Install a compatible uv, e.g.:\n"
        "  curl -LsSf https://github.com/astral-sh/uv/releases/download/0.11.26/"
        "uv-installer.sh | sh\n"
        "or ensure a compatible install (e.g. Homebrew's /opt/homebrew/bin/uv) "
        "is reachable on PATH.",
    )
    return "\n".join(lines)


def main() -> int:
    """CLI: print the resolved absolute path to stdout, or fail with remediation.

    On success, prints *only* the resolved path (nothing else) and exits 0,
    so a caller can capture it via `$(...)`. On failure, prints nothing to
    stdout, prints remediation to stderr, and exits 1.
    """
    try:
        candidate = find_compatible_uv_for_repo()
    except NoCompatibleUvError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(candidate.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
