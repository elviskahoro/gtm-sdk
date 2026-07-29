"""Fail the build when a symbol a downstream repo imports stops existing.

gtm-sdk is a library whose consumers live in other repos, so its own test suite
and its own static analysis both see that surface as unreferenced. Eight
bot-authored "remove unused code" PRs (#362-#371) acted on exactly that
inference and proposed deleting seven symbols that ``ai/projects/crm-uploader``
calls in production. Nothing in CI would have caught it.

This test closes that gap by resolving every ``(module, symbol)`` pair recorded
in ``contracts/downstream_api.toml``. It cannot clone the consumer — gtm-sdk is
public and ``ai/`` is private — so the contract travels as committed data,
regenerated locally by ``scripts/downstream-contract-sync.py``.

The failure message lists the full blast radius in one run. A broad deletion
should surface as one report, not as a bisect one symbol at a time.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

# `uv run pytest` does not chdir, so anchor on the file, not the CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = REPO_ROOT / "contracts" / "downstream_api.toml"

SYNC_HINT = (
    "If the symbol moved or was intentionally removed, update the consumer "
    "first, then re-run scripts/downstream-contract-sync.py --write. Do not "
    "edit the contract to make this test pass."
)


def _load_contract() -> dict[str, dict[str, list[str]]]:
    with CONTRACT_PATH.open("rb") as handle:
        data = tomllib.load(handle)

    consumers = data.get("consumers")
    if not consumers:
        pytest.fail(
            f"{CONTRACT_PATH.relative_to(REPO_ROOT)} declares no consumers. An "
            f"empty contract silently disables this guard.",
        )

    return {
        consumer: entry.get("modules", {}) for consumer, entry in consumers.items()
    }


CONTRACT = _load_contract()


def _iter_entries() -> Iterator[tuple[str, str, list[str]]]:
    for consumer, modules in sorted(CONTRACT.items()):
        for module, symbols in sorted(modules.items()):
            yield consumer, module, symbols


def test_contract_file_exists() -> None:
    assert CONTRACT_PATH.is_file(), (
        f"{CONTRACT_PATH.relative_to(REPO_ROOT)} is missing. Regenerate it with "
        f"scripts/downstream-contract-sync.py <consumer-checkout> --write."
    )


def test_every_declared_symbol_resolves() -> None:
    """Import each recorded module and resolve each recorded symbol."""
    failures: list[str] = []
    for consumer, module_path, symbols in _iter_entries():
        try:
            module = importlib.import_module(module_path)
        except ImportError as error:
            failures.append(
                f"{consumer}: cannot import {module_path} ({error})",
            )
            continue

        failures.extend(
            f"{consumer}: {module_path}.{symbol} no longer exists"
            for symbol in sorted(symbols)
            if not hasattr(module, symbol)
        )

    assert not failures, (
        "Downstream consumers import symbols that this repo no longer "
        f"provides:\n  " + "\n  ".join(failures) + f"\n\n{SYNC_HINT}"
    )


def test_public_symbols_stay_in_package_all() -> None:
    """A consumed public symbol must remain in its package's ``__all__``.

    ``__all__`` is the signal dead-code tooling reads as "this is public API"
    (the bot PRs say so verbatim), so a PR that strips an entry while leaving
    the definition in place re-opens the hole without breaking any import. That
    is the shape of PR #368, which removed ``fetch_blog_posts`` from
    ``libs/sanity``'s ``__all__``.

    Only applies to package roots (``libs.attio``, not ``libs.attio.companies``)
    and only to public names — consumers reach into a few private helpers, which
    stay out of ``__all__`` on purpose.
    """
    failures: list[str] = []
    for consumer, module_path, symbols in _iter_entries():
        package_root = ".".join(module_path.split(".")[:2])
        try:
            package: Any = importlib.import_module(package_root)
        except ImportError:
            # test_every_declared_symbol_resolves already reports this.
            continue

        declared = getattr(package, "__all__", None)
        if declared is None:
            continue

        failures.extend(
            f"{consumer}: {symbol} (consumed via {module_path}) is missing from "
            f"{package_root}.__all__"
            for symbol in sorted(symbols)
            if not symbol.startswith("_")
            and hasattr(package, symbol)
            and symbol not in declared
        )

    assert not failures, (
        "Consumed public symbols dropped out of their package's public API "
        f"declaration:\n  " + "\n  ".join(failures) + f"\n\n{SYNC_HINT}"
    )


def test_declared_exports_are_importable() -> None:
    """Every name in a consumed package's ``__all__`` must actually resolve.

    Guards the inverse mistake: deleting a definition but leaving its
    ``__all__`` entry behind, which turns ``from libs.attio import *`` into an
    ``AttributeError`` at consumer import time.
    """
    package_roots = {
        ".".join(module_path.split(".")[:2]) for _, module_path, _ in _iter_entries()
    }
    failures: list[str] = []
    for package_root in sorted(package_roots):
        try:
            package: Any = importlib.import_module(package_root)
        except ImportError:
            continue

        declared = getattr(package, "__all__", None)
        if declared is None:
            continue

        failures.extend(
            f"{package_root}.__all__ names {symbol}, which does not exist"
            for symbol in declared
            if not hasattr(package, symbol)
        )

    assert not failures, "\n  ".join(["Dangling __all__ entries:", *failures])
