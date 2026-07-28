"""Static invariants for the CI-failure-triage workflow.

Each guard here corresponds to a specific way this workflow could regress into
something harmful or silently useless. They parse the YAML rather than
substring-matching it, so commenting a step out fails the guard instead of
quietly satisfying it.

The `if:`-expression guard is the important one. `warpdotdev/oz-agent-action`'s
own `fix-failing-checks.yml` ships this gate:

    if:
      ${{ github.event.workflow_run.conclusion == 'failure' }} &&
      !startsWith(github.event.workflow_run.head_branch, 'oz-agent-fix/')

Mixing a `${{ }}` replacement token with literal text makes GitHub evaluate the
whole value as a template, yielding a non-empty (therefore truthy) string. That
gate never blocks: it fires on SUCCESSFUL runs and its recursion guard is dead.
GitHub has warned on the pattern since 2026-01-29. Copying that shape into this
repo is the regression this file exists to prevent.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "agent-ci-triage.yml"
PIPELINE = REPO_ROOT / ".github" / "workflows" / "ci" / "triage_dagger.py"
FILING_SCRIPT = REPO_ROOT / "scripts" / "ci-triage-linear-issue.py"

# Kept in lockstep with tests-unit.yml / tests-integration.yml and with the
# dagger-io pin the workflow installs.
DAGGER_VERSION = "0.21.7"


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    # Keys are not all `str`: PyYAML resolves the bare `on:` key to the boolean
    # True, so the mapping is genuinely heterogeneous.
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def job(workflow: dict[Any, Any]) -> dict[str, Any]:
    return workflow["jobs"]["triage"]


@pytest.fixture(scope="module")
def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job["steps"]


def _step(steps: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    for step in steps:
        if str(step.get("name", "")).startswith(prefix):
            return step
    msg = f"no step whose name starts with {prefix!r}"
    raise AssertionError(msg)


def test_triggers_only_on_completed_workflow_runs(workflow: dict[Any, Any]) -> None:
    # PyYAML parses a bare `on` key as the boolean True.
    triggers: dict[str, Any] = workflow.get("on") or workflow[True]
    assert list(triggers) == ["workflow_run"]
    assert triggers["workflow_run"]["types"] == ["completed"]
    # Names must be workflow names, not filenames or job names.
    assert "Unit tests" in triggers["workflow_run"]["workflows"]
    assert "Integration tests" in triggers["workflow_run"]["workflows"], (
        "the nightly suite is the motivating case: 13 consecutive red nights"
    )


def test_gate_is_an_expression_not_an_interpolated_string(job: dict[str, Any]) -> None:
    """The upstream always-truthy bug. See the module docstring."""
    assert "${{" not in job["if"], (
        "an `if:` mixing ${{ }} with literal text is a truthy STRING and never blocks"
    )


def test_gate_requires_failure_and_blocks_forks_and_recursion(
    job: dict[str, Any],
) -> None:
    condition = " ".join(job["if"].split())
    assert "github.event.workflow_run.conclusion == 'failure'" in condition, (
        "types: [completed] also fires on success"
    )
    assert (
        "github.event.workflow_run.head_repository.full_name == github.repository"
        in condition
    ), (
        "workflow_run runs in the BASE repo with secrets even for fork PRs; "
        "without this guard a fork's failing CI reaches our credentials"
    )
    assert "startsWith(github.event.workflow_run.head_branch" in condition, (
        "missing recursion guard"
    )


def test_permissions_stay_minimal(
    workflow: dict[Any, Any],
    job: dict[str, Any],
) -> None:
    assert workflow["permissions"] == {"contents": "read"}
    perms = job["permissions"]
    assert perms.get("contents") == "read", "triage never commits"
    assert "issues" not in perms, "Linear is the reporting channel, not GitHub Issues"
    assert perms.get("actions") == "read", "needs the failed run's logs"


def test_checkout_never_materializes_the_failing_head(
    steps: list[dict[str, Any]],
) -> None:
    """Checking out the triggering commit in a secret-bearing workflow_run job is
    the workflow-run-target-code-checkout weakness semgrep flags."""
    checkouts = [s for s in steps if "actions/checkout" in str(s.get("uses", ""))]
    assert checkouts, "expected a checkout step"
    for step in checkouts:
        with_block = step.get("with") or {}
        assert "ref" not in with_block, (
            "must check out the default branch, not the failing head"
        )
        assert with_block.get("persist-credentials") is False


def test_agent_step_holds_no_credentials(steps: list[dict[str, Any]]) -> None:
    """The agent's only output is a file, so it needs no token at all.

    This is what makes a prompt-injected diagnosis unable to spend or exfiltrate
    the Linear key or act on GitHub.
    """
    agent = _step(steps, "Diagnose with Oz")
    env = agent.get("env") or {}
    leaked = [k for k in env if any(t in k for t in ("TOKEN", "API_KEY", "GH_"))]
    assert leaked == [], f"agent step must hold no credentials, found {leaked}"


def test_no_github_context_interpolated_into_shell(steps: list[dict[str, Any]]) -> None:
    """Run metadata is attacker-influencable and `${{ }}` is substituted before the
    shell parses it (semgrep run-shell-injection). It must arrive via `env:`."""
    offenders = [
        step.get("name")
        for step in steps
        if "${{ github." in str(step.get("run") or "")
    ]
    assert offenders == [], offenders


def test_dagger_is_the_primary_filing_path(steps: list[dict[str, Any]]) -> None:
    setup = _step(steps, "Setup Dagger")
    assert setup["with"]["version"] == DAGGER_VERSION, (
        "pin the CLI: unpinned, an upstream release silently changes the engine"
    )
    sdk = _step(steps, "Install Dagger Python SDK")
    assert f"dagger-io=={DAGGER_VERSION}" in sdk["run"], (
        "the SDK pin must match the CLI pin"
    )
    dagger_step = _step(steps, "File or bump the Linear issue (Dagger)")
    assert "dagger run python" in dagger_step["run"]
    assert ".github/workflows/ci/triage_dagger.py" in dagger_step["run"]


def test_host_fallback_exists_and_only_runs_when_dagger_failed(
    steps: list[dict[str, Any]],
) -> None:
    """registry.dagger.io brownouts (dagger/dagger#7548) already force six retries
    in tests-integration.yml. A reporter that dies with the registry is useless
    exactly when CI is unhappy."""
    fallback = _step(steps, "File or bump the Linear issue (host fallback)")
    condition = " ".join(str(fallback["if"]).split())
    assert "steps.linear_dagger.outcome != 'success'" in condition
    assert "python3 scripts/ci-triage-linear-issue.py" in fallback["run"]

    dagger_step = _step(steps, "File or bump the Linear issue (Dagger)")
    assert dagger_step.get("continue-on-error") is True, (
        "the Dagger step must not fail the job, or the fallback never runs"
    )


def test_no_linear_team_secret_is_required(steps: list[dict[str, Any]]) -> None:
    """The team is hard-coded in the filing script, deliberately."""
    for step in steps:
        env = step.get("env") or {}
        assert "LINEAR_TEAM_ID" not in env, (
            "the team is hard-coded as LINEAR_TEAM in the filing script"
        )
    assert "LINEAR_TEAM = " in FILING_SCRIPT.read_text(encoding="utf-8")


def test_preflight_gates_on_both_credentials(steps: list[dict[str, Any]]) -> None:
    """Merging before the secrets exist must not turn one red check into two."""
    preflight = _step(steps, "Preflight credentials")
    run = preflight["run"]
    assert "WARP_API_KEY" in run
    assert "LINEAR_API_KEY" in run
    for step in steps:
        name = str(step.get("name", ""))
        if name.startswith(("Diagnose", "File or bump", "Setup Dagger")):
            assert "steps.preflight.outputs.configured" in str(step.get("if", "")), (
                f"{name} must be gated on the preflight"
            )


def test_pipeline_mounts_only_the_filing_script(steps: list[dict[str, Any]]) -> None:
    """A broken lockfile or poisoned tree must not influence the filing step --
    this pipeline runs precisely because something in CI is already broken."""
    source = PIPELINE.read_text(encoding="utf-8")
    assert 'dag.host().directory("scripts", include=[SCRIPT_NAME])' in source
    assert "with_secret_variable" in source, "the key must not be an image layer"

    # Exactly one exec, and it runs the filing script directly. No dependency
    # install step: the script is stdlib-only, so there is nothing to resolve --
    # which is the point, since `uv sync` is itself a plausible cause of the
    # failure being triaged. (Checked on the code, not the prose: the module
    # docstring legitimately mentions `uv sync` while explaining its absence.)
    execs = [line for line in source.splitlines() if ".with_exec(" in line]
    assert len(execs) == 1, f"expected a single with_exec, found {execs}"
    assert "sh" in execs[0], "the exec wraps the script so its exit code is captured"
    for forbidden in ("uv sync", "pip install", "apt-get"):
        assert forbidden not in source.split('"""', 2)[-1], (
            f"{forbidden!r} in the pipeline body defeats the stdlib-only design"
        )
