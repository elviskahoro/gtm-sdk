# trunk-ignore-all(pyright/reportUnusedFunction): autouse pytest fixtures are invoked by name
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "fireflies-attio_meetings-backfill.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "fireflies_backfill_attio_meetings",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubOp:
    """Minimal op stand-in: enough for _describe_op's fallback branch."""

    op_type = "upsert_meeting"

    def model_dump(self) -> dict[str, Any]:
        return {}


def _meeting_outcome(*, record_id: str, action: str, matched: bool) -> SimpleNamespace:
    meta: dict[str, Any] = {"output_schema_version": "v1"}
    if matched:
        meta["matched_existing"] = True
    return SimpleNamespace(
        # OpOutcome.op_type is the op class name (type(op).__name__), not the
        # snake_case AttioOp.op_type — see src/attio/export.py.
        op_type="UpsertMeeting",
        record_id=record_id,
        envelope=SimpleNamespace(action=action, meta=meta),
    )


def _full_scope(api_key: str | None = None) -> tuple[bool, set[str], str]:
    del api_key
    return (
        True,
        {"record_permission:read-write", "object_configuration:read-write"},
        "test-workspace",
    )


@pytest.fixture(autouse=True)
def _stub_attio_scope_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """--execute runs assert_attio_token_scopes; stub it so no GET /v2/self fires."""
    import libs.attio.preflight as _preflight

    _preflight.reset_scope_cache()
    monkeypatch.setenv("ATTIO_API_KEY", "stub-attio-key-for-tests")
    monkeypatch.setattr(_preflight, "fetch_token_scopes", _full_scope)


def _stub_source(
    module: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    rows: list[dict[str, str]],
) -> None:
    """Wire connect/iter/map so the script reads ``rows`` and emits one op each."""
    monkeypatch.setattr(module, "TMP_DIR", tmp_path)

    import libs.motherduck as motherduck_mod

    def _connect(*_a: object, **_k: object) -> object:
        return object()

    def _iter(_con: object) -> Iterator[dict[str, str]]:
        return iter(rows)

    def _from_row(raw: dict[str, str]) -> dict[str, str]:
        return raw

    def _to_ops(_rec: object, **_k: object) -> list[_StubOp]:
        return [_StubOp()]

    monkeypatch.setattr(motherduck_mod, "connect", _connect)
    monkeypatch.setattr(module, "iter_assembled_rows", _iter)
    monkeypatch.setattr(module, "from_motherduck_row", _from_row)
    monkeypatch.setattr(module, "to_attio_operations", _to_ops)


def test_execute_report_surfaces_action_and_matched_existing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The --execute report must expose per-op action + matched_existing — the
    empirical dedup proof for the smoke test (ai-av8)."""
    module = _load_script_module()
    _stub_source(
        module,
        monkeypatch,
        tmp_path,
        rows=[{"id": "ff-1"}, {"id": "ff-2"}],
    )

    pending = [
        # recording 1 collapsed onto a pre-existing meeting (the dedup win)
        _meeting_outcome(record_id="mtg_existing", action="noop", matched=True),
        # recording 2 minted a new dlt-mtg- meeting
        _meeting_outcome(record_id="mtg_new", action="created", matched=False),
    ]
    # main() does a deferred ``from src.attio.export import execute`` at call
    # time, so patching the source module (not a module-level ``module.execute``,
    # which does not exist) is what the script actually resolves. The call
    # counter below proves the stub is the function the script invoked.
    calls: list[object] = []
    import src.attio.export as export_mod

    def _execute(plan: object) -> SimpleNamespace:
        calls.append(plan)
        return SimpleNamespace(
            success=True,
            fail_index=None,
            fail_reason=None,
            outcomes=[pending.pop(0)],
        )

    monkeypatch.setattr(export_mod, "execute", _execute)
    monkeypatch.setattr("sys.argv", ["prog", "--execute"])

    rc = module.main()

    out = capsys.readouterr().out
    assert rc == 0
    # The stub ran once per recording — the report reflects real execute() output.
    assert len(calls) == 2
    # Per-op observability lines.
    assert "action=noop matched_existing=True record_id=mtg_existing" in out
    assert "action=created matched_existing=False record_id=mtg_new" in out
    # Roll-up tally line distinguishing the dedup outcomes.
    assert "matched_existing=1 via_find_or_create=1" in out
    report = next(tmp_path.glob("fireflies-backfill-*.md"))
    assert "matched_existing=True" in report.read_text(encoding="utf-8")


def test_failed_meeting_not_counted_in_tally(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A failed UpsertMeeting (action=failed) must not be bucketed as a
    successful meeting outcome — it belongs only in the failed count."""
    module = _load_script_module()
    _stub_source(module, monkeypatch, tmp_path, rows=[{"id": "ff-1"}])

    import src.attio.export as export_mod

    failed_meeting = SimpleNamespace(
        op_type="UpsertMeeting",
        record_id=None,
        envelope=SimpleNamespace(action="failed", meta={}, errors=[]),
    )

    def _execute(_plan: object) -> SimpleNamespace:
        return SimpleNamespace(
            success=False,
            fail_index=0,
            fail_reason="op_failed",
            outcomes=[failed_meeting],
        )

    monkeypatch.setattr(export_mod, "execute", _execute)
    monkeypatch.setattr("sys.argv", ["prog", "--execute"])

    rc = module.main()

    out = capsys.readouterr().out
    assert rc == 1
    assert "failed=1" in out
    # The failed meeting is neither matched nor a find-or-create success.
    assert "matched_existing=0 via_find_or_create=0" in out


def test_dry_run_omits_outcome_lines(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Dry run never calls execute(), so it must not emit outcome/tally lines."""
    module = _load_script_module()
    _stub_source(module, monkeypatch, tmp_path, rows=[{"id": "ff-1"}])

    import src.attio.export as export_mod

    def _fail_execute(_plan: object) -> SimpleNamespace:  # pragma: no cover
        raise AssertionError("execute() must not be called in a dry run")

    monkeypatch.setattr(export_mod, "execute", _fail_execute)
    monkeypatch.setattr("sys.argv", ["prog"])

    rc = module.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out
    assert "matched_existing" not in out
    assert "action=" not in out
