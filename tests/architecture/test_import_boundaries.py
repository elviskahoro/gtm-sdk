"""Tests for the dependency boundaries between adapters and orchestration."""

from __future__ import annotations

import ast
from pathlib import Path


def _repo_root() -> Path:
    """Return the source root in an editable checkout or Bazel runfiles tree."""

    return Path(__file__).resolve().parents[2]


def _import_names(tree: ast.AST) -> list[tuple[int, str]]:
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            imports.append((node.lineno, module))
            if module == "libs":
                imports.extend(
                    (node.lineno, f"libs.{alias.name}") for alias in node.names
                )
    return imports


def _violations(root: Path) -> list[str]:
    violations: list[str] = []
    libs_root = root / "libs"
    for path in sorted(libs_root.rglob("*.py")):
        relative = path.relative_to(root)
        parts = relative.parts
        if len(parts) < 3:
            continue
        adapter = parts[1]
        tree = ast.parse(path.read_text(), filename=str(relative))
        for line, imported in _import_names(tree):
            if imported == "src" or imported.startswith("src."):
                violations.append(f"{relative}:{line} -> {imported}")
            elif imported == "cli" or imported.startswith("cli."):
                violations.append(f"{relative}:{line} -> {imported}")
            elif imported == "libs" or not imported.startswith("libs."):
                continue
            elif len(imported.split(".")) < 3:
                # ``libs.telemetry`` and similar root helpers are shared
                # support modules, not adapter-to-adapter dependencies.
                continue
            elif imported.split(".")[1] != adapter:
                violations.append(f"{relative}:{line} -> {imported}")
    return violations


def test_adapter_import_boundaries_are_acyclic() -> None:
    violations = _violations(_repo_root())

    assert not violations, "Python adapter import boundary violations:\n" + "\n".join(
        violations,
    )
