"""Adapter import-boundary contract.

PR 2 (Task 8, ai-m4p9.1) removes the two audited Python graph violations
*before* Bazel/Gazelle generates first-party dependency edges, so the libs
graph stays acyclic with one ownership unit per adapter:

1. ``libs.attio.errors`` imported orchestration state (``MODAL_APP``) from
   ``src.modal_app``. Adapters must be callable in isolation and never reach
   into ``src`` orchestration — that coordination belongs in ``src``.
2. The ``libs.dlt`` adapter imported ``DestinationFileData`` from the
   ``libs.filesystem`` adapter. Two adapters must not depend on each other; the
   DLT adapter now types its writable-file parameter against the
   ``WritableFile`` structural protocol in ``libs.dlt.filesystem_types`` so the
   concrete ``DestinationFileData`` (still owned by ``libs.filesystem``) crosses
   the boundary structurally at call sites in ``src`` instead of as an import.

These tests are written test-first: they fail on the current violations and
pass once the imports are replaced. The ``libs -> src`` guard is general (only
the Attio adapter violated it). The cross-adapter guard is scoped to the DLT
adapter, which is the one that imported a sibling adapter's type.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIBS = REPO_ROOT / "libs"


def _imported_modules(path: Path) -> list[str]:
    """Dotted module names imported by ``path`` (top-level and nested).

    Relative imports (``node.level > 0``) resolve within the adapter's own
    package, so they are ignored — they can never cross an adapter boundary.
    """
    tree = ast.parse(path.read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.append(node.module)
    return names


def _lib_files(subdir: str | None = None) -> list[Path]:
    root = LIBS if subdir is None else LIBS / subdir
    return sorted(root.rglob("*.py"))


def test_libs_do_not_import_orchestration() -> None:
    """No adapter (``libs/*``) may import orchestration (``src.*``)."""
    offenders = [
        (str(p.relative_to(REPO_ROOT)), name)
        for p in _lib_files()
        for name in _imported_modules(p)
        if name == "src" or name.startswith("src.")
    ]
    assert not offenders, (
        f"libs adapters must not import src orchestration; found: {offenders}"
    )


def test_dlt_adapter_does_not_import_sibling_adapters() -> None:
    """``libs.dlt`` must not depend on another ``libs.<adapter>`` package.

    The audited violation was ``libs.dlt`` importing ``DestinationFileData``
    from ``libs.filesystem``. ``libs.dlt.*`` intra-package imports are allowed.
    """
    offenders = []
    for path in _lib_files("dlt"):
        for name in _imported_modules(path):
            if name == "libs" or name.startswith("libs."):
                adapter = name.split(".")[1] if name != "libs" else ""
                if adapter != "dlt":
                    offenders.append((str(path.relative_to(REPO_ROOT)), name))
    assert not offenders, (
        f"libs.dlt must not import sibling adapters; found: {offenders}"
    )
