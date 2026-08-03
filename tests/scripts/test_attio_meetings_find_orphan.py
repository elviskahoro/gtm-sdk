"""Regression tests for the read-only Attio orphan-meeting CLI."""

# ruff: noqa: S101 -- pytest assertions are intentional.

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest

# Deferred annotations keep this import type-checking-only at runtime.
if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "attio-meetings-find_orphan.py"
MODULE_NAME = "attio_meetings_find_orphan"


@pytest.fixture
def script_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def test_summary_logs_counts_without_sensitive_token_data(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    candidates = [
        SimpleNamespace(created_by_type="system"),
        SimpleNamespace(created_by_type="api-token"),
        SimpleNamespace(created_by_type="other"),
    ]

    def fake_iter_meetings(**_: object) -> Iterator[SimpleNamespace]:
        return iter(candidates)

    def fake_detect_orphans(_: object) -> list[object]:
        return []

    def fake_classify(_: object) -> tuple[list[object], list[object]]:
        return [], []

    def fake_write_orphan_csvs(
        _: object,
        __: Path,
    ) -> dict[str, Path]:
        return {
            "confident": tmp_path / "confident.csv",
            "review": tmp_path / "review.csv",
            "all": tmp_path / "all.csv",
        }

    monkeypatch.setattr(
        "libs.attio.preflight.fetch_token_scopes",
        lambda: (True, {"meeting:read"}, "dlthub"),
    )
    monkeypatch.setattr(
        script_module,
        "iter_meetings_in_range",
        fake_iter_meetings,
    )
    monkeypatch.setattr(script_module, "detect_orphans", fake_detect_orphans)
    monkeypatch.setattr(script_module, "classify", fake_classify)
    monkeypatch.setattr(script_module, "write_orphan_csvs", fake_write_orphan_csvs)
    monkeypatch.setattr(
        sys,
        "argv",
        ["attio-meetings-find_orphan.py", "--output-dir", str(tmp_path)],
    )

    assert script_module.main() == 0

    output = capsys.readouterr().out
    assert "# scanned=3 system=1 api-token=1 other=1" in output
    assert "password" not in output.lower()
    assert "secret" not in output.lower()


@pytest.mark.parametrize("option", ["--start", "--end"])
def test_invalid_date_is_a_usage_error(
    script_module: ModuleType,
    option: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert script_module.main([option, "not-a-date"]) == 2  # noqa: PLR2004
    assert option in capsys.readouterr().err  # noqa: S101
