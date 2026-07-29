#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Regenerate ``contracts/downstream_api.toml`` from a consumer checkout.

gtm-sdk is a library. Its downstream consumers live in *other* repos (notably
the private ``ai/`` repo's ``projects/crm-uploader``), so nothing inside this
repo references the symbols they import. Static dead-code analysis therefore
reports load-bearing public API as unused — which is how eight bot-authored
"remove unused code" PRs came to propose deleting seven symbols that
``crm-uploader`` calls in production.

``contracts/downstream_api.toml`` is the answer: a committed record of which
symbols downstream repos import, enforced by
``tests/test_downstream_contract.py``. This script keeps that record honest by
deriving it mechanically from a consumer checkout instead of by hand.

It is deliberately **not** wired into CI: gtm-sdk is public and the consumer is
private, so public CI can never see the consumer tree. Run it locally whenever a
consumer's imports change, then commit the result.

    scripts/downstream-contract-sync.py ~/Documents/ai/projects/crm-uploader
    scripts/downstream-contract-sync.py ~/Documents/ai/projects/crm-uploader --write

Needs no secrets and makes no network calls, so there is no ``infisical run``
wrapper here.

Why ``ast`` and not grep: consumers hide imports inside functions to break
cycles or defer heavy modules (``crm-uploader``'s ``src/tam/workflow.py`` does
exactly this with ``find_person_by_name_at_company``). A line-oriented scan
aimed at module headers misses those, and a missed symbol is precisely the
failure this file exists to prevent.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
import tomllib
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CONTRACT_PATH = REPO_ROOT / "contracts" / "downstream_api.toml"

# Top-level packages this repo publishes (see `[tool.setuptools.packages.find]`
# in pyproject.toml). An import whose root is one of these resolves into
# gtm-sdk, so it belongs in the contract.
SDK_ROOTS = ("libs", "src", "cli")

# Column at which trunk's TOML formatter breaks an inline array onto its own
# lines. Rendering must match it exactly — see render_contract.
TOML_FORMATTER_WIDTH = 80

# Consumers vendor their own venvs and caches; those trees are full of imports
# that are not the consumer's own code.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "tmp",
        "build",
        "dist",
    },
)

CONTRACT_HEADER = """\
# Symbols that downstream repos import from gtm-sdk.
#
# A symbol listed here is public API: it may be entirely unreferenced inside
# this repo and still be load-bearing. Do not delete one because a dead-code
# scan called it unused — see AGENTS.md, "Public API and downstream consumers".
#
# Generated file. Regenerate rather than hand-edit:
#   scripts/downstream-contract-sync.py <consumer-checkout> --write
#
# Enforced by tests/test_downstream_contract.py, which runs in the required
# `Unit tests` gate.
"""


class ConsumerScanError(Exception):
    """A consumer checkout could not be scanned."""


def _iter_python_files(root: Path) -> list[Path]:
    """Yield the consumer's own ``.py`` files, skipping vendored trees."""
    found: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if SKIP_DIRS.intersection(path.relative_to(root).parts):
            continue

        found.append(path)

    return found


def _sdk_root(module: str) -> str | None:
    """Return the gtm-sdk top-level package ``module`` resolves into, if any."""
    root, _, _ = module.partition(".")
    return root if root in SDK_ROOTS else None


def scan_consumer(root: Path) -> dict[str, set[str]]:
    """Map each imported gtm-sdk module to the symbol names taken from it.

    A bare ``import libs.attio.people`` contributes the module with no symbols,
    which still pins the module's importability. ``from libs.attio import x``
    contributes ``x``. Relative imports are the consumer's own and ignored.
    """
    if not root.is_dir():
        msg = f"consumer checkout is not a directory: {root}"
        raise ConsumerScanError(msg)

    imports: dict[str, set[str]] = {}
    for path in _iter_python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as error:
            # A consumer may hold intentionally-broken fixtures; one unparseable
            # file must not silently truncate the whole contract.
            print(f"warning: skipping unparseable {path}: {error}", file=sys.stderr)
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # `node.level > 0` is a relative import: consumer-local.
                if node.level or node.module is None:
                    continue

                if _sdk_root(node.module) is None:
                    continue

                imports.setdefault(node.module, set()).update(
                    alias.name for alias in node.names if alias.name != "*"
                )

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _sdk_root(alias.name) is not None:
                        imports.setdefault(alias.name, set())

    if not imports:
        msg = (
            f"no gtm-sdk imports found under {root} — wrong path, or the "
            f"consumer no longer depends on {'/'.join(SDK_ROOTS)}"
        )
        raise ConsumerScanError(msg)

    return imports


def _consumer_name(root: Path) -> str:
    return root.resolve().name


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_contract(
    consumers: dict[str, dict[str, set[str]]],
    descriptions: dict[str, str],
) -> str:
    """Render the full TOML document.

    Hand-rolled rather than via a TOML writer: the stdlib ships no serializer,
    and the shape here (two nested tables, string arrays) is small enough that a
    third-party dependency would cost more than it saves. Everything is sorted
    so a regeneration produces a reviewable diff instead of a reshuffle.
    """
    chunks = [CONTRACT_HEADER]
    for consumer in sorted(consumers):
        modules = consumers[consumer]
        chunks.append(f"\n[consumers.{_quote(consumer)}]\n")
        description = descriptions.get(consumer)
        if description:
            chunks.append(f"description = {_quote(description)}\n")

        chunks.append(f"\n[consumers.{_quote(consumer)}.modules]\n")
        for module in sorted(modules):
            symbols = sorted(modules[module])
            if not symbols:
                chunks.append(f"{_quote(module)} = []\n")
                continue

            rendered = ", ".join(_quote(symbol) for symbol in symbols)
            line = f"{_quote(module)} = [{rendered}]\n"
            # Wrap at the same column trunk's TOML formatter does. If these two
            # disagree, `trunk fmt` rewrites the generated file and this script
            # then reports the contract as permanently stale.
            if len(line.rstrip("\n")) <= TOML_FORMATTER_WIDTH:
                chunks.append(line)
                continue

            chunks.append(f"{_quote(module)} = [\n")
            chunks.extend(f"  {_quote(symbol)},\n" for symbol in symbols)
            chunks.append("]\n")

    return "".join(chunks)


def load_existing() -> tuple[dict[str, dict[str, set[str]]], dict[str, str]]:
    """Read the committed contract, so unscanned consumers survive a sync."""
    if not CONTRACT_PATH.is_file():
        return {}, {}

    with CONTRACT_PATH.open("rb") as handle:
        data = tomllib.load(handle)

    consumers: dict[str, dict[str, set[str]]] = {}
    descriptions: dict[str, str] = {}
    for consumer, entry in data.get("consumers", {}).items():
        consumers[consumer] = {
            module: set(symbols) for module, symbols in entry.get("modules", {}).items()
        }
        description = entry.get("description")
        if description:
            descriptions[consumer] = description

    return consumers, descriptions


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "consumers",
        nargs="+",
        type=Path,
        help="paths to downstream consumer checkouts to scan",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="apply the change (default: print a diff and exit non-zero if stale)",
    )
    args = parser.parse_args()

    consumers, descriptions = load_existing()
    for path in args.consumers:
        try:
            scanned = scan_consumer(path)
        except ConsumerScanError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

        name = _consumer_name(path)
        # Replace, not merge: a symbol the consumer stopped importing should
        # leave the contract, or the contract becomes an append-only ratchet
        # that pins API surface nobody uses.
        consumers[name] = scanned
        descriptions.setdefault(name, f"downstream consumer: {name}")
        print(
            f"scanned {name}: {len(scanned)} modules, "
            f"{sum(len(symbols) for symbols in scanned.values())} symbols",
            file=sys.stderr,
        )

    rendered = render_contract(consumers, descriptions)
    current = (
        CONTRACT_PATH.read_text(encoding="utf-8") if CONTRACT_PATH.is_file() else ""
    )
    if rendered == current:
        print(f"{CONTRACT_PATH.relative_to(REPO_ROOT)} is up to date")
        return 0

    if args.write:
        CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT_PATH.write_text(rendered, encoding="utf-8")
        print(f"wrote {CONTRACT_PATH.relative_to(REPO_ROOT)}")
        return 0

    sys.stdout.writelines(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=f"a/{CONTRACT_PATH.relative_to(REPO_ROOT)}",
            tofile=f"b/{CONTRACT_PATH.relative_to(REPO_ROOT)}",
        ),
    )
    print("\ncontract is stale; re-run with --write to apply", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
