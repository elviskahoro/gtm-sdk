"""Tests for scripts/ci-triage-diagnose.py.

The Oz client is stubbed throughout -- no test makes a network call, and none needs
a WARP_API_KEY.

Two behaviours carry the weight here:

- **Polling terminates.** A cloud run is asynchronous, and `RunItem` exposes no
  final-message field, so the script polls to a terminal state before it can read
  anything. A poll loop that missed a terminal state, or ignored its deadline, would
  hang the job until the 20-minute workflow timeout.
- **Output extraction degrades.** Artifacts are the only channel for text. If the
  agent produces nothing usable the script must exit 0 having written no file, so the
  caller still files a log-only issue. Silence is the failure mode being designed
  against, not a tolerable outcome.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci-triage-diagnose.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location("ci_triage_diagnose", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script() -> Any:
    return _load_script_module()


class _Obj:
    """Minimal stand-in for the SDK's pydantic models (attribute access only)."""

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def _plan_artifact(uid: str = "plan-1") -> _Obj:
    return _Obj(artifact_type="PLAN", data=_Obj(artifact_uid=uid))


def _file_artifact(uid: str, filename: str) -> _Obj:
    return _Obj(artifact_type="FILE", data=_Obj(artifact_uid=uid, filename=filename))


class _FakeAgent:
    def __init__(self, outer: _FakeClient) -> None:
        self._outer = outer
        self.runs = _FakeRuns(outer)

    def run(self, **kwargs: Any) -> _Obj:
        self._outer.run_kwargs = kwargs
        return _Obj(run_id="run-123", state="QUEUED")

    def get_artifact(self, uid: str) -> Any:
        return self._outer.artifact_responses[uid]

    def list_environments(self) -> str:
        return "env-list"


class _FakeRuns:
    def __init__(self, outer: _FakeClient) -> None:
        self._outer = outer

    def retrieve(self, run_id: str) -> Any:  # noqa: ARG002
        self._outer.polls += 1
        return self._outer.states.pop(0) if self._outer.states else self._outer.final


class _FakeClient:
    def __init__(
        self,
        *,
        states: list[Any] | None = None,
        final: Any = None,
        artifact_responses: dict[str, Any] | None = None,
    ) -> None:
        self.states = list(states or [])
        self.final = (
            final if final is not None else _Obj(state="SUCCEEDED", artifacts=[])
        )
        self.artifact_responses = artifact_responses or {}
        self.run_kwargs: dict[str, Any] = {}
        self.polls = 0
        self.agent = _FakeAgent(self)


def _no_sleep(_seconds: float) -> None:
    """Injected for `sleep` so poll tests do not spend real seconds."""


def _frozen_clock() -> float:
    return 0.0


def _echo_url(url: str) -> str:
    """Injected for `fetch_url`: returns the URL so tests can assert on it."""
    return url


def _unreachable_fetch(_url: str) -> str:
    msg = "fetch_url must not be called when a PLAN artifact is available"
    raise AssertionError(msg)


def _args(output: Path, log: Path | None = None, diff: Path | None = None) -> list[str]:
    argv = [
        "--repo",
        "o/r",
        "--workflow",
        "Unit tests",
        "--run-url",
        "https://github.com/o/r/actions/runs/9",
        "--branch",
        "main",
        "--commit",
        "abc1234",
        "--event",
        "schedule",
        "--output",
        str(output),
    ]
    if log is not None:
        argv += ["--log-file", str(log)]
    if diff is not None:
        argv += ["--diff-file", str(diff)]
    return argv


# --------------------------------------------------------------------------- prompt


def test_prompt_embeds_log_and_diff_and_forbids_repo_access(script: Any) -> None:
    prompt = script.build_prompt(
        repo="o/r",
        workflow="Unit tests",
        run_url="https://x/9",
        branch="main",
        commit="abc",
        event="schedule",
        log="taplo/error: invalid TOML",
        diff="--- a/x\n+++ b/x",
    )
    assert "taplo/error: invalid TOML" in prompt
    assert "--- a/x" in prompt
    # The agent has no filesystem; the prompt must say so or it will try to read one.
    # Collapse whitespace: the instruction is wrapped across lines in the template.
    flat = " ".join(prompt.split())
    assert "NO access to the repository working tree" in flat
    assert "Co-Authored-By" in prompt, "the trailer ban must reach the agent"


def test_prompt_omits_the_diff_section_when_there_is_no_diff(script: Any) -> None:
    prompt = script.build_prompt(
        repo="o/r",
        workflow="W",
        run_url="u",
        branch="main",
        commit="abc",
        event="push",
        log="boom",
        diff="",
    )
    assert "Diff that introduced the failure" not in prompt
    assert "boom" in prompt


def test_log_is_tail_capped(script: Any, tmp_path: Path) -> None:
    """The error is at the END of a log, and the whole prompt crosses the wire."""
    path = tmp_path / "log"
    path.write_text("A" * 50 + "TAIL_MARKER", encoding="utf-8")
    out = script._read_capped(path, 11)
    assert out == "TAIL_MARKER"


def test_missing_evidence_files_are_tolerated(script: Any, tmp_path: Path) -> None:
    assert script._read_capped(tmp_path / "nope", 100) == ""
    assert script._read_capped(None, 100) == ""


# --------------------------------------------------------------------------- polling


def test_poll_stops_at_every_terminal_state(script: Any) -> None:
    """A state the loop does not recognise as terminal hangs the job to timeout."""
    for state in ("SUCCEEDED", "FAILED", "ERROR", "CANCELLED", "BLOCKED"):
        client = _FakeClient(final=_Obj(state=state, artifacts=[]))
        run = script.poll_until_terminal(
            client,
            "run-123",
            timeout_seconds=60,
            sleep=_no_sleep,
            now=_frozen_clock,
        )
        assert run.state == state
        assert client.polls == 1, state


def test_poll_keeps_going_through_non_terminal_states(script: Any) -> None:
    client = _FakeClient(
        states=[
            _Obj(state="QUEUED", artifacts=[]),
            _Obj(state="CLAIMED", artifacts=[]),
            _Obj(state="INPROGRESS", artifacts=[]),
        ],
        final=_Obj(state="SUCCEEDED", artifacts=[]),
    )
    slept: list[int] = []
    run = script.poll_until_terminal(
        client,
        "run-123",
        timeout_seconds=600,
        interval_seconds=7,
        sleep=slept.append,
        now=_frozen_clock,
    )
    assert run.state == "SUCCEEDED"
    assert client.polls == 4
    assert slept == [7, 7, 7]


def test_poll_gives_up_at_the_deadline(script: Any) -> None:
    """Must not spin until the workflow's own 20-minute timeout kills it."""
    client = _FakeClient(
        states=[_Obj(state="INPROGRESS", artifacts=[]) for _ in range(50)],
        final=_Obj(state="INPROGRESS", artifacts=[]),
    )
    clock = iter([0.0, 0.0, 999.0, 999.0])
    run = script.poll_until_terminal(
        client,
        "run-123",
        timeout_seconds=10,
        sleep=_no_sleep,
        now=clock.__next__,
    )
    assert run.state == "INPROGRESS"
    assert client.polls < 50, "should have bailed at the deadline"


# ------------------------------------------------------------------ artifact extract


def test_plan_artifact_content_is_preferred(script: Any) -> None:
    """PLAN embeds markdown in the response, so it needs no download and no file
    write inside the sandbox -- the most reliable of the three paths.
    """
    client = _FakeClient(
        artifact_responses={
            "plan-1": _Obj(data=_Obj(content="  # diagnosis from plan  ")),
        },
    )
    run = _Obj(artifacts=[_plan_artifact("plan-1")])
    got = script.extract_diagnosis(
        client,
        run,
        want_filename="diagnosis.md",
        fetch_url=_unreachable_fetch,
    )
    assert got == "# diagnosis from plan"


def test_file_artifact_is_downloaded_when_there_is_no_plan(script: Any) -> None:
    client = _FakeClient(
        artifact_responses={
            "f1": _Obj(data=_Obj(download_url="https://signed/1")),
        },
    )
    run = _Obj(artifacts=[_file_artifact("f1", "diagnosis.md")])
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return "from the file artifact"

    got = script.extract_diagnosis(
        client,
        run,
        want_filename="diagnosis.md",
        fetch_url=fetch,
    )
    assert got == "from the file artifact"
    assert fetched == ["https://signed/1"]


def test_exact_filename_wins_over_other_markdown(script: Any) -> None:
    client = _FakeClient(
        artifact_responses={
            "other": _Obj(data=_Obj(download_url="https://signed/other")),
            "want": _Obj(data=_Obj(download_url="https://signed/want")),
        },
    )
    run = _Obj(
        artifacts=[
            _file_artifact("other", "notes.md"),
            _file_artifact("want", "diagnosis.md"),
        ],
    )
    got = script.extract_diagnosis(
        client,
        run,
        want_filename="diagnosis.md",
        fetch_url=_echo_url,
    )
    assert got == "https://signed/want"


def test_non_markdown_files_are_ignored(script: Any) -> None:
    client = _FakeClient(
        artifact_responses={"bin": _Obj(data=_Obj(download_url="https://signed/bin"))},
    )
    run = _Obj(artifacts=[_file_artifact("bin", "screenshot.png")])
    assert (
        script.extract_diagnosis(
            client,
            run,
            want_filename="diagnosis.md",
            fetch_url=_echo_url,
        )
        == ""
    )


def test_artifact_errors_are_survivable(script: Any) -> None:
    """A broken artifact must degrade to 'no diagnosis', never crash the job."""

    class _Boom:
        def get_artifact(self, uid: str) -> Any:
            msg = "artifact gone"
            raise RuntimeError(msg)

        list_environments = None

    client = _Obj(agent=_Boom())
    run = _Obj(artifacts=[_plan_artifact("plan-1")])
    assert (
        script.extract_diagnosis(
            client,
            run,
            want_filename="diagnosis.md",
            fetch_url=_echo_url,
        )
        == ""
    )


def test_no_artifacts_yields_empty(script: Any) -> None:
    client = _FakeClient()
    assert (
        script.extract_diagnosis(
            client,
            _Obj(artifacts=None),
            want_filename="diagnosis.md",
            fetch_url=_echo_url,
        )
        == ""
    )


# ------------------------------------------------------------------------------ main


def test_main_writes_the_diagnosis(
    script: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(script.time, "sleep", _no_sleep)
    out = tmp_path / "diagnosis.md"
    log = tmp_path / "log"
    log.write_text("taplo/error: invalid TOML", encoding="utf-8")
    client = _FakeClient(
        final=_Obj(state="SUCCEEDED", artifacts=[_plan_artifact("plan-1")]),
        artifact_responses={"plan-1": _Obj(data=_Obj(content="root cause: escapes"))},
    )

    assert script.main(_args(out, log=log), client=client) == 0
    assert out.read_text(encoding="utf-8").strip() == "root cause: escapes"
    # plan mode is what makes the PLAN artifact path available at all.
    assert client.run_kwargs["mode"] == "plan"
    assert client.run_kwargs["interactive"] is False
    assert "taplo/error: invalid TOML" in client.run_kwargs["prompt"]


def test_main_writes_nothing_when_no_diagnosis_and_still_succeeds(
    script: Any,
    tmp_path: Path,
) -> None:
    """Exit 0 with no file: the caller files a log-only issue instead. Turning this
    into a failure would make one red check into two.
    """
    out = tmp_path / "diagnosis.md"
    client = _FakeClient(final=_Obj(state="FAILED", artifacts=[]))
    assert script.main(_args(out), client=client) == 0
    assert not out.exists()


def test_main_flags_a_non_success_terminal_state_in_the_output(
    script: Any,
    tmp_path: Path,
) -> None:
    """A diagnosis from a FAILED run may be truncated; the reader must be told."""
    out = tmp_path / "diagnosis.md"
    client = _FakeClient(
        final=_Obj(state="BLOCKED", artifacts=[_plan_artifact("plan-1")]),
        artifact_responses={"plan-1": _Obj(data=_Obj(content="partial finding"))},
    )
    assert script.main(_args(out), client=client) == 0
    body = out.read_text(encoding="utf-8")
    assert "partial finding" in body
    assert "BLOCKED" in body


def test_config_omits_unset_fields(script: Any) -> None:
    """An empty environment_id must be absent, not sent as '' -- the API treats a
    missing one as 'no environment', which is the documented default.
    """
    args = script.parse_args(
        [*_args(Path("out.md")), "--environment-id", "", "--harness", ""],
    )
    config = script.build_config(args)
    assert "environment_id" not in config
    assert "harness" not in config
    assert config["name"] == "ci-triage-Unit tests"


def test_config_includes_environment_and_harness_when_given(script: Any) -> None:
    args = script.parse_args(
        [
            *_args(Path("out.md")),
            "--environment-id",
            "env-abc",
            "--harness",
            "codex",
            "--model-id",
            "some-model",
        ],
    )
    config = script.build_config(args)
    assert config["environment_id"] == "env-abc"
    assert config["harness"] == {"type": "codex"}
    assert config["model_id"] == "some-model"


def test_active_cli_is_registered_as_a_typer_command(script: Any) -> None:  # noqa: ANN401
    assert [command.name for command in script.app.registered_commands] == ["diagnose"]  # noqa: S101


def test_run_start_failure_is_reported_as_exit_one(
    script: Any,
    tmp_path: Path,
) -> None:
    class _Failing:
        runs = None

        def run(self, **kwargs: Any) -> Any:
            msg = "402 payment required"
            raise RuntimeError(msg)

    client = _Obj(agent=_Failing())
    assert script.main(_args(tmp_path / "d.md"), client=client) == 1


# ------------------------------------------------------------------- normalization


def test_normalization_strips_warp_prose_escaping(script: Any) -> None:
    r"""Warp's plan renderer escapes prose punctuation; Linear renders it literally.

    Verified against a live run, which emitted `taplo \\(invoked by ...\\)` and
    `high\\-severity`.
    """
    got = script.normalize_agent_markdown(
        r"taplo \(invoked by trunk\) is high\-severity\.",
    )
    assert got == "taplo (invoked by trunk) is high-severity."


def test_normalization_never_touches_fenced_blocks(script: Any) -> None:
    r"""The whole point of one real diagnosis was an invalid `\\d` escape -- mangling
    code would destroy the very detail that matters.
    """
    text = "prose \\(x\\)\n```text\nregex `[A-Z]+-\\d+` \\(kept\\)\n```\nmore \\(y\\)"
    got = script.normalize_agent_markdown(text)
    assert r"[A-Z]+-\d+" in got, "backslash inside a fence must survive"
    assert r"\(kept\)" in got, "fence content is byte-for-byte"
    assert "prose (x)" in got
    assert "more (y)" in got


def test_normalization_never_touches_inline_code_spans(script: Any) -> None:
    got = script.normalize_agent_markdown(r"prose \(x\) and `a\-b` and \-dash")
    assert got == r"prose (x) and `a\-b` and -dash"


def test_normalization_leaves_emphasis_escapes_alone(script: Any) -> None:
    """A backslash before * or _ is usually load-bearing, so it is not in the set."""
    text = r"a \*not bold\* b \_u\_"
    assert script.normalize_agent_markdown(text) == text


def test_normalization_rewrites_the_warp_fence_language(script: Any) -> None:
    """`warp-runnable-command` is Warp-specific and renders as an unknown language."""
    got = script.normalize_agent_markdown("```warp-runnable-command\nls -l\n```")
    assert "warp-runnable-command" not in got
    assert got.startswith("```text")
    assert "ls -l" in got


def test_normalization_preserves_other_fence_languages(script: Any) -> None:
    got = script.normalize_agent_markdown("```diff\n-a\n+b\n```")
    assert got.startswith("```diff")


def test_extraction_normalizes_plan_content(script: Any) -> None:
    """Normalization must be wired into the extraction path, not just available."""
    client = _FakeClient(
        artifact_responses={"plan-1": _Obj(data=_Obj(content=r"root cause \(taplo\)"))},
    )
    got = script.extract_diagnosis(
        client,
        _Obj(artifacts=[_plan_artifact("plan-1")]),
        want_filename="diagnosis.md",
        fetch_url=_echo_url,
    )
    assert got == "root cause (taplo)"
