"""Tests for scripts/ci-triage-linear-issue.py.

The script turns a triage agent's diagnosis into exactly one Linear issue per
failing workflow. The behaviour that actually matters is dedupe: a recurring
failure must bump the existing issue rather than file a duplicate. The nightly
Integration tests were red 13 consecutive nights, so a per-occurrence filing
policy would have produced 13 tickets for one problem.

The ``libs.linear.client`` seam is stubbed throughout — no test touches the
Linear API. Stubs hand back real ``gtm_linear`` models rather than duck-typed
stand-ins, so a field the script reads that the SDK stopped returning fails here
instead of in production.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from gtm_linear import Issue, IssueCreateInput, IssueUpdateInput, Team
from gtm_linear._generated.fragments import IssueFieldsState
from gtm_linear.exceptions import GraphQLError, LinearGraphQLError, LinearHTTPError

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci-triage-linear-issue.py"

TEAM_UUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
MARKER = "<!-- ci-triage-key: Unit tests -->"


def _issue(
    *,
    id_: str,
    identifier: str,
    url: str,
    title: str = "ci: Unit tests is failing",
    description: str | None = None,
) -> Issue:
    """A real ``IssueFields``, so field drift in the SDK surfaces here."""
    return Issue(
        id=id_,
        identifier=identifier,
        title=title,
        description=description,
        url=url,
        priority=0.0,
        state=IssueFieldsState(id="state-1", name="Todo", type="unstarted"),
        assignee=None,
    )


def _as_linear_stores_it(description: str) -> str:
    """Round-trip a description through Linear's markdown normalisation.

    Linear rewrites ``- `` list markers to ``* `` on write, so a footer read
    back never looks like the one ``build_footer`` produced. Feeding tests the
    literal written form hid that for the whole life of the script: AI-1 and
    AI-2 were both pinned at "Occurrences: 2" in production while every test
    here happily counted upwards.
    """
    return re.sub(r"^- ", "* ", description, flags=re.MULTILINE)


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ci_triage_linear_issue",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script() -> Any:
    return _load_script_module()


def _install_fake_adapter(
    module: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: list[Issue] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Answer every ``libs.linear.client`` call locally. Returns the call log."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def get_team_by_key(key: str, *, api_key: str) -> Team | None:  # noqa: ARG001
        calls.append(("get_team_by_key", {"key": key}))
        return Team(id=TEAM_UUID, name="Engineering", key="ENG")

    def list_open_team_issues(
        team_id: str,
        *,
        first: int,
        api_key: str,  # noqa: ARG001
    ) -> list[Issue]:
        calls.append(("list_open_team_issues", {"teamId": team_id, "first": first}))
        return list(existing or [])

    def create_issue(input_: IssueCreateInput, *, api_key: str) -> Issue:  # noqa: ARG001
        calls.append(
            (
                "create_issue",
                {
                    "teamId": input_.team_id,
                    "title": input_.title,
                    "description": input_.description,
                },
            ),
        )
        return _issue(
            id_="issue-1",
            identifier="ENG-42",
            url="https://linear.app/ENG-42",
            description=input_.description,
        )

    def update_issue(
        issue_id: str,
        update: IssueUpdateInput,
        *,
        api_key: str,  # noqa: ARG001
    ) -> Issue:
        calls.append(
            ("update_issue", {"id": issue_id, "description": update.description}),
        )
        return _issue(
            id_=issue_id,
            identifier="ENG-7",
            url="https://linear.app/ENG-7",
            description=update.description,
        )

    monkeypatch.setattr(module.linear, "get_team_by_key", get_team_by_key)
    monkeypatch.setattr(module.linear, "list_open_team_issues", list_open_team_issues)
    monkeypatch.setattr(module.linear, "create_issue", create_issue)
    monkeypatch.setattr(module.linear, "update_issue", update_issue)
    return calls


def _args(
    diagnosis: Path,
    output: Path,
    *,
    team: str | None = TEAM_UUID,
) -> list[str]:
    """Build a CLI invocation. ``team=None`` exercises the hard-coded default."""
    args = [
        "--workflow",
        "Unit tests",
        "--run-url",
        "https://github.com/o/r/actions/runs/9",
        "--branch",
        "main",
        "--commit",
        "abc1234",
        "--timestamp",
        "2026-07-27T00:00:00Z",
        "--diagnosis-file",
        str(diagnosis),
        "--output",
        str(output),
    ]
    if team is not None:
        args += ["--team", team]
    return args


@pytest.fixture
def diagnosis(tmp_path: Path) -> Path:
    path = tmp_path / "diagnosis.md"
    path.write_text("Root cause: taplo rejected an invalid escape.", encoding="utf-8")
    return path


def _set_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API key is the only credential; the team is hard-coded in the script."""
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")


def test_missing_api_key_exits_two(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    diagnosis: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    assert script.main(_args(diagnosis, tmp_path / "out.tsv")) == 2


def test_team_defaults_to_the_hard_coded_constant(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    diagnosis: Path,
    tmp_path: Path,
) -> None:
    """No --team and no env var: the script must still know where to file."""
    _set_creds(monkeypatch)
    calls = _install_fake_adapter(script, monkeypatch)

    assert script.main(_args(diagnosis, tmp_path / "out.tsv", team=None)) == 0

    resolve = next(v for n, v in calls if n == "get_team_by_key")
    assert resolve["key"] == script.LINEAR_TEAM
    assert script.LINEAR_TEAM, "a team must actually be hard-coded"


def test_missing_diagnosis_file_is_not_an_error(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A flaky agent step must not turn one red check into two."""
    _set_creds(monkeypatch)
    calls = _install_fake_adapter(script, monkeypatch)
    rc = script.main(_args(tmp_path / "absent.md", tmp_path / "out.tsv"))
    assert rc == 0
    assert calls == []


def test_empty_diagnosis_file_is_not_an_error(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_creds(monkeypatch)
    calls = _install_fake_adapter(script, monkeypatch)
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    assert script.main(_args(empty, tmp_path / "out.tsv")) == 0
    assert calls == []


def test_creates_issue_when_none_exists(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    diagnosis: Path,
    tmp_path: Path,
) -> None:
    _set_creds(monkeypatch)
    calls = _install_fake_adapter(script, monkeypatch)
    output = tmp_path / "out.tsv"

    assert script.main(_args(diagnosis, output, team="ENG")) == 0

    ops = [name for name, _ in calls]
    assert ops == ["get_team_by_key", "list_open_team_issues", "create_issue"]

    payload = next(v for n, v in calls if n == "create_issue")
    assert payload["teamId"] == TEAM_UUID, "team key must be resolved to a UUID"
    assert payload["title"] == "ci: Unit tests is failing"
    assert "Occurrences: 1" in payload["description"]
    assert MARKER in payload["description"], "marker enables future dedupe"
    assert output.read_text(encoding="utf-8").strip() == (
        "ENG-42\thttps://linear.app/ENG-42"
    )


def test_team_uuid_skips_resolution(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    diagnosis: Path,
    tmp_path: Path,
) -> None:
    _set_creds(monkeypatch)
    calls = _install_fake_adapter(script, monkeypatch)
    assert script.main(_args(diagnosis, tmp_path / "out.tsv")) == 0
    assert "get_team_by_key" not in [name for name, _ in calls]


def test_unknown_team_key_fails_loudly(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    diagnosis: Path,
    tmp_path: Path,
) -> None:
    _set_creds(monkeypatch)

    def no_such_team(key: str, *, api_key: str) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(script.linear, "get_team_by_key", no_such_team)
    # A key (not a UUID) so resolution actually runs and can come back empty.
    assert script.main(_args(diagnosis, tmp_path / "out.tsv", team="NOPE")) == 1


def test_recurrence_bumps_existing_issue_instead_of_duplicating(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    diagnosis: Path,
    tmp_path: Path,
) -> None:
    """The 13-consecutive-nights case: one issue, a rising occurrence count."""
    prior_description = "\n\n".join(
        [
            "Root cause: a human edited this text.",
            _as_linear_stores_it(
                script.build_footer(
                    marker=MARKER,
                    occurrences=3,
                    run_url="https://github.com/o/r/actions/runs/1",
                    branch="main",
                    commit="oldsha",
                    first_seen="2026-07-15T00:00:00Z",
                    latest_seen="2026-07-26T00:00:00Z",
                ),
            ),
        ],
    )
    existing = [
        _issue(
            id_="issue-9",
            identifier="ENG-7",
            url="https://linear.app/ENG-7",
            description=prior_description,
        ),
    ]
    _set_creds(monkeypatch)
    calls = _install_fake_adapter(script, monkeypatch, existing=existing)
    output = tmp_path / "out.tsv"

    assert script.main(_args(diagnosis, output)) == 0

    ops = [name for name, _ in calls]
    assert "create_issue" not in ops, "must not duplicate"
    assert "update_issue" in ops

    updated = next(v for n, v in calls if n == "update_issue")["description"]
    occurrences = re.search(r"Occurrences: (\d+)", updated)
    assert occurrences is not None
    assert occurrences.group(1) == "4"
    assert "First seen: 2026-07-15T00:00:00Z" in updated, "first_seen is preserved"
    assert "Last seen: 2026-07-27T00:00:00Z" in updated
    assert "a human edited this text" in updated, "human prose is never rewritten"
    assert updated.count(script.FOOTER_SENTINEL) == 1, "footer must not accumulate"
    assert output.read_text(encoding="utf-8").strip() == (
        "ENG-7\thttps://linear.app/ENG-7"
    )


def test_marker_is_scoped_per_workflow(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    diagnosis: Path,
    tmp_path: Path,
) -> None:
    """An open issue for a different workflow must not absorb this failure."""
    existing = [
        _issue(
            id_="issue-other",
            identifier="ENG-1",
            url="https://linear.app/ENG-1",
            title="ci: Docs checks is failing",
            description="something\n<!-- ci-triage-key: Docs checks -->",
        ),
    ]
    _set_creds(monkeypatch)
    calls = _install_fake_adapter(script, monkeypatch, existing=existing)

    assert script.main(_args(diagnosis, tmp_path / "out.tsv")) == 0
    assert "create_issue" in [name for name, _ in calls]


def test_split_body_drops_only_the_managed_footer(script: Any) -> None:
    body = "Diagnosis text.\n\n" + script.build_footer(
        marker=MARKER,
        occurrences=1,
        run_url="u",
        branch="b",
        commit="c",
        first_seen="t",
        latest_seen="t",
    )
    assert script.split_body(body) == "Diagnosis text."


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            LinearGraphQLError(
                "rate limited",
                errors=[GraphQLError(message="rate limited")],
            ),
            id="graphql-error",
        ),
        pytest.param(LinearHTTPError("boom", status_code=500, body=""), id="http-500"),
        # Transport failures are NOT wrapped by gtm_linear, so they reach the
        # script as raw httpx. Losing this catch turns exit 1 into a traceback.
        pytest.param(httpx.ConnectError("no route to host"), id="transport"),
        # A 200 whose payload does not match the generated model.
        pytest.param(ValueError("API did not return an issue object"), id="no-issue"),
    ],
)
def test_sdk_failures_surface_as_exit_one(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    diagnosis: Path,
    tmp_path: Path,
    error: Exception,
) -> None:
    _set_creds(monkeypatch)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(script.linear, "list_open_team_issues", boom)
    assert script.main(_args(diagnosis, tmp_path / "out.tsv")) == 1
