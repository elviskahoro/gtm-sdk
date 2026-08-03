#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["typer>=0.15"]
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

import pathlib as _pathlib
import sys as _sys

_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
from scripts.lib.uv_bootstrap import bootstrap_uv as _bootstrap_uv  # noqa: E402

if __name__ == "__main__":
    _bootstrap_uv(script_path=__file__, mode="script")

import ast
import difflib
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from collections.abc import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CONTRACT_PATH = REPO_ROOT / "contracts" / "downstream_api.toml"

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=__doc__,
)

# Top-level packages this repo publishes (see `[tool.setuptools.packages.find]`
# in pyproject.toml).
#
# `src` and `cli` are dangerously generic: consumers routinely have their own
# top-level `src/` and `cli/` (crm-uploader does, and so does dlt-hub/gtm-os),
# so matching on the root name alone would record a consumer's *own* modules as
# SDK dependencies and pin surface this repo never had. Every candidate is
# therefore resolved against both trees — see `_resolves_into_sdk`.
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


def _module_exists_in(tree_root: Path, module: str) -> bool:
    """Does ``module`` correspond to a file or package under ``tree_root``?"""
    relative = Path(*module.split("."))
    return (tree_root / relative).is_dir() or (
        tree_root / relative.with_suffix(".py")
    ).is_file()


def _resolves_into_sdk(module: str, consumer_root: Path) -> bool:
    """Decide whether ``module`` names a gtm-sdk module rather than a local one.

    Root-name matching alone is not enough: ``src`` and ``cli`` exist in both
    trees. A module counts as SDK surface only if it exists here *and* not in
    the consumer, so an ambiguous name resolves the way Python would at runtime
    with the consumer's own package on the path first.
    """
    root, _, _ = module.partition(".")
    if root not in SDK_ROOTS:
        return False

    if not _module_exists_in(REPO_ROOT, module):
        return False

    return not _module_exists_in(consumer_root, module)


def _dotted_name(node: ast.expr) -> str | None:
    """Flatten an attribute chain into ``"a.b.c"``, or ``None`` if not a chain.

    ``libs.attio.people.upsert_person`` parses as nested ``Attribute`` nodes over
    a ``Name``; recovering the dotted string is what lets a bare qualified import
    be matched against the modules this repo publishes.
    """
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value

    if not isinstance(current, ast.Name):
        return None

    parts.append(current.id)
    return ".".join(reversed(parts))


def _collect_symbols_via_attribute_access(
    tree: ast.AST,
    bound_modules: dict[str, str],
) -> dict[str, set[str]]:
    """Recover symbols reached by attribute access on a bound module.

    Three import styles bind a module rather than a symbol, and none of them
    names the consumed symbols on the import node itself::

        import libs.attio.people as people   # people.upsert_person()
        from libs.attio import people        # people.upsert_person()
        import libs.attio.people             # libs.attio.people.upsert_person()

    Without this pass the contract records the module and leaves every symbol
    reached through it unprotected — precisely the gap the contract exists to
    close. ``bound_modules`` maps the local dotted prefix to the SDK module it
    refers to.

    The chain can be deeper than the bound prefix plus one component: ``import
    libs.attio`` followed by ``libs.attio.companies.find_company_by_domain()``
    binds only ``libs.attio``, so the intermediate ``companies`` has to be walked
    as a submodule before the trailing name is recognised as the symbol. Matching
    only ``prefix.rpartition(".")`` misses that call entirely *and* records
    ``companies`` as a symbol of ``libs.attio`` — which, being a submodule, is
    absent from ``__all__`` and would fail the contract's own guard.
    """
    found: dict[str, set[str]] = {}
    prefixes = sorted(bound_modules, key=len, reverse=True)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue

        dotted = _dotted_name(node)
        if dotted is None:
            continue

        # Longest match first: a consumer can bind both `libs.attio` and
        # `libs.attio.people` in the same file.
        prefix = next(
            (candidate for candidate in prefixes if dotted.startswith(f"{candidate}.")),
            None,
        )
        if prefix is None:
            continue

        module = bound_modules[prefix]
        rest = dotted[len(prefix) + 1 :].split(".")
        # Consume leading components that name submodules, so the remainder is
        # an attribute of the deepest real module.
        while len(rest) > 1 and _module_exists_in(REPO_ROOT, f"{module}.{rest[0]}"):
            module = f"{module}.{rest[0]}"
            rest.pop(0)

        symbol = rest[0]
        if _module_exists_in(REPO_ROOT, f"{module}.{symbol}"):
            # An intermediate node of a longer chain (`libs.attio.companies` on
            # the way to `...find_company_by_domain`). The full chain's own node
            # records the symbol; recording the submodule name here would put a
            # non-``__all__`` name into the contract.
            continue

        found.setdefault(module, set()).add(symbol)

    return found


def scan_consumer(root: Path) -> dict[str, set[str]]:
    """Map each imported gtm-sdk module to the symbol names taken from it.

    Covers every import style that can consume SDK surface: ``from X import a``,
    ``import X.Y``, ``import X.Y as z``, and ``from X import submodule`` — the
    last three bind a module, so their symbols are recovered from attribute
    access (see ``_collect_symbols_via_attribute_access``). Relative imports are
    consumer-local and ignored.

    A submodule imported via ``from libs.attio import people`` is recorded as its
    own module entry, *not* as a symbol of ``libs.attio``. Submodules are
    deliberately absent from a package's ``__all__``, so recording one as a
    symbol would make the contract fail its own ``__all__`` guard.
    """
    if not root.is_dir():
        msg = f"consumer checkout is not a directory: {root}"
        raise ConsumerScanError(msg)

    root = root.resolve()
    imports: dict[str, set[str]] = {}
    for path in _iter_python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as error:
            # Hard error, not a warning: this scan's output is written over the
            # committed contract, so tolerating a skipped file means `--write`
            # can silently drop the protection that file's imports established.
            msg = f"cannot parse {path}: {error}"
            raise ConsumerScanError(msg) from error

        # Local dotted prefix -> SDK module, for the attribute-access pass below.
        bound_modules: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # `node.level > 0` is a relative import: consumer-local.
                if node.level or node.module is None:
                    continue

                if not _resolves_into_sdk(node.module, root):
                    continue

                for alias in node.names:
                    if alias.name == "*":
                        # A star import consumes whatever the module exports, so
                        # there is no honest symbol list to record. Refuse rather
                        # than silently pin nothing.
                        msg = (
                            f"{path} star-imports {node.module}; the contract "
                            f"cannot record which symbols that consumes. Replace "
                            f"it with explicit imports in the consumer."
                        )
                        raise ConsumerScanError(msg)

                    submodule = f"{node.module}.{alias.name}"
                    if _module_exists_in(REPO_ROOT, submodule):
                        # Binds a module, not a symbol.
                        imports.setdefault(submodule, set())
                        bound_modules[alias.asname or alias.name] = submodule
                        continue

                    imports.setdefault(node.module, set()).add(alias.name)

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if not _resolves_into_sdk(alias.name, root):
                        continue

                    imports.setdefault(alias.name, set())
                    # `import X.Y as z` binds `z`; bare `import X.Y` binds `X`,
                    # and usage reads through the full dotted path.
                    bound_modules[alias.asname or alias.name] = alias.name

        for module, symbols in _collect_symbols_via_attribute_access(
            tree,
            bound_modules,
        ).items():
            imports.setdefault(module, set()).update(symbols)

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


def sync_contract(consumer_paths: Sequence[Path], *, write: bool) -> int:
    consumers, descriptions = load_existing()
    for path in consumer_paths:
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

    if write:
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


@app.command()
def cli(
    consumers: Annotated[
        list[Path],
        typer.Argument(help="paths to downstream consumer checkouts to scan"),
    ],
    *,
    write: Annotated[
        bool,
        typer.Option(
            "--write",
            help="apply the change (default: print a diff and exit non-zero if stale)",
        ),
    ] = False,
) -> int:
    return sync_contract(consumers, write=write)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = app(
            args=list(argv) if argv is not None else None,
            standalone_mode=False,
        )
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1

    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
