from __future__ import annotations

import ast
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest

FORBIDDEN_ORCHESTRATION_ROOTS = {"cli", "src"}
EXPECTED_TRACKED_LIB_FILES = 132
MIN_ADAPTER_PATH_PARTS = 3
MIN_LIBS_MODULE_PARTS = 2

if TYPE_CHECKING:
    from collections.abc import Iterator


class ImportReference(NamedTuple):
    module: str
    line: int


class BoundaryViolation(NamedTuple):
    path: str
    line: int
    module: str


def _candidate_roots() -> Iterator[Path]:
    yield Path(__file__).resolve().parents[2]

    test_srcdir = os.environ.get("TEST_SRCDIR")
    test_workspace = os.environ.get("TEST_WORKSPACE")
    if test_srcdir and test_workspace:
        yield Path(test_srcdir) / test_workspace

    runfiles_dir = os.environ.get("RUNFILES_DIR")
    if runfiles_dir and test_workspace:
        yield Path(runfiles_dir) / test_workspace


def _repo_root() -> Path:
    seen: set[Path] = set()
    for candidate in _candidate_roots():
        root = candidate.resolve()
        if root in seen:
            continue
        seen.add(root)
        if (root / "libs").is_dir():
            return root

    candidates = ", ".join(str(path) for path in seen)
    pytest.fail(f"could not locate repository root; checked: {candidates}")


def _tracked_lib_files(root: Path) -> list[Path]:
    git = shutil.which("git")
    if git is not None and (root / ".git").exists():
        result = subprocess.run(  # noqa: S603,S607 -- fixed git argv, shell disabled.
            [git, "-C", str(root), "ls-files", "libs/*.py", "libs/**/*.py"],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode == 0:
            return [root / path for path in result.stdout.splitlines()]

    return sorted(path for path in (root / "libs").rglob("*.py") if path.is_file())


def _adapter_for_path(relative_path: Path) -> str | None:
    parts = relative_path.parts
    if len(parts) < MIN_ADAPTER_PATH_PARTS or parts[0] != "libs":
        return None
    return parts[1]


def _libs_adapter_from_module(module: str, root: Path) -> str | None:
    parts = module.split(".")
    if len(parts) < MIN_LIBS_MODULE_PARTS or parts[0] != "libs":
        return None

    candidate = parts[1]
    if (root / "libs" / f"{candidate}.py").is_file():
        return None
    if (root / "libs" / candidate).is_dir():
        return candidate
    return None


def _imported_modules(tree: ast.AST) -> Iterator[ImportReference]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield ImportReference(module=alias.name, line=node.lineno)
            continue

        if (
            not isinstance(node, ast.ImportFrom)
            or node.level != 0
            or node.module is None
        ):
            continue

        if node.module in {"libs", "src", "cli"}:
            for alias in node.names:
                if alias.name == "*":
                    continue
                yield ImportReference(
                    module=f"{node.module}.{alias.name}",
                    line=node.lineno,
                )
            continue

        yield ImportReference(module=node.module, line=node.lineno)


def _is_orchestration_import(module: str) -> bool:
    root = module.split(".", 1)[0]
    return root in FORBIDDEN_ORCHESTRATION_ROOTS


def _boundary_violations(
    root: Path,
    source_paths: list[Path],
) -> list[BoundaryViolation]:
    violations: list[BoundaryViolation] = []
    for source_path in source_paths:
        relative_path = source_path.relative_to(root)
        source_adapter = _adapter_for_path(relative_path)
        if source_adapter is None:
            continue

        tree = ast.parse(
            source_path.read_text(encoding="utf-8"),
            filename=str(source_path),
        )
        for imported in _imported_modules(tree):
            imported_adapter = _libs_adapter_from_module(imported.module, root)
            if imported_adapter is not None and imported_adapter != source_adapter:
                violations.append(
                    BoundaryViolation(
                        path=relative_path.as_posix(),
                        line=imported.line,
                        module=imported.module,
                    ),
                )
                continue

            if _is_orchestration_import(imported.module):
                violations.append(
                    BoundaryViolation(
                        path=relative_path.as_posix(),
                        line=imported.line,
                        module=imported.module,
                    ),
                )

    return sorted(violations)


def test_lib_adapters_do_not_import_other_adapters_or_orchestration() -> None:
    root = _repo_root()
    source_paths = _tracked_lib_files(root)
    if len(source_paths) != EXPECTED_TRACKED_LIB_FILES:
        pytest.fail(
            "tracked libs source scan found "
            f"{len(source_paths)} files; expected {EXPECTED_TRACKED_LIB_FILES}",
        )

    violations = _boundary_violations(root, source_paths)
    formatted = [
        f"{violation.path}:{violation.line} -> {violation.module}"
        for violation in violations
    ]

    if formatted:
        pytest.fail("Adapter import boundary violations:\n" + "\n".join(formatted))
