"""Keep frozen Tach package interfaces aligned with package declarations."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TACH_PATH = REPO_ROOT / "tach.toml"

# These package roots were flattened by the module-boundary migration. DLT's
# GCP implementation remains an intentional dedicated-module escape hatch for
# handlers that explicitly need the optional gcsfs-backed functionality.
FLATTENED_PACKAGES = {
    "libs.apollo",
    "libs.attio",
    "libs.dlt",
    "libs.exa",
    "libs.granola",
    "libs.octolens",
    "libs.parallel",
    "libs.parsers",
    "libs.sanity",
    "libs.webhook",
}
INTENTIONAL_EXPORTS = {
    "libs.dlt": {
        "filesystem_gcp",
        r"filesystem_gcp\.CloudGoogle",
        r"filesystem_gcp\.GCPCredentials",
    },
}


def _package_all(package: str) -> set[str]:
    package_path = REPO_ROOT / Path(package.replace(".", "/")) / "__init__.py"
    tree = ast.parse(package_path.read_text())
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        )
    )
    value: Any = ast.literal_eval(assignment.value)
    assert isinstance(value, list)  # noqa: S101
    assert all(isinstance(name, str) for name in value)  # noqa: S101
    return set(value)


def _tach_package_exports() -> dict[str, set[str]]:
    config = tomllib.loads(TACH_PATH.read_text())
    exports: dict[str, set[str]] = {}
    for interface in config["interfaces"]:
        sources = interface.get("from", [])
        if len(sources) != 1:
            continue
        match = re.fullmatch(r"(libs\.[a-z_]+)", sources[0].replace("\\.", "."))
        if match and match.group(1) in FLATTENED_PACKAGES:
            exports[match.group(1)] = set(interface["expose"])
    return exports


def test_flattened_tach_interfaces_match_package_all() -> None:
    """Tach must expose package declarations, not retained submodule names."""
    configured = _tach_package_exports()
    failures: list[str] = []
    for package in sorted(FLATTENED_PACKAGES):
        expected = _package_all(package) | INTENTIONAL_EXPORTS.get(package, set())
        actual = configured.get(package)
        if actual is None:
            failures.append(f"{package}: missing Tach interface")
            continue
        if actual != expected:
            failures.append(
                f"{package}: Tach={sorted(actual)}; __all__={sorted(expected)}",
            )

    assert not failures, "Tach/package public-interface drift:\n  " + "\n  ".join(  # noqa: S101
        failures,
    )
