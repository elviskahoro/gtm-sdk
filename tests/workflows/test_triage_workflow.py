# ruff: noqa: S101 -- asserts are the point of a test file.

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

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "agent-ci-triage.yml"
PIPELINE = REPO_ROOT / ".github" / "workflows" / "ci" / "triage_dagger.py"
FILING_SCRIPT = REPO_ROOT / "scripts" / "ci-triage-linear-issue.py"
FILING_SCRIPT_LOCK = REPO_ROOT / "scripts" / "ci-triage-linear-issue.py.lock"
GTM_LINEAR_CONSTRAINTS = (
    REPO_ROOT / ".github" / "workflows" / "ci" / "constraints" / "gtm-linear.txt"
)

DIAGNOSE_PIPELINE = (
    REPO_ROOT / ".github" / "workflows" / "ci" / "triage_diagnose_dagger.py"
)
DIAGNOSE_SCRIPT = REPO_ROOT / "scripts" / "ci-triage-diagnose.py"
DIAGNOSE_SCRIPT_LOCK = REPO_ROOT / "scripts" / "ci-triage-diagnose.py.lock"
OZ_SDK_CONSTRAINTS = (
    REPO_ROOT / ".github" / "workflows" / "ci" / "constraints" / "oz-agent-sdk.txt"
)

# Kept in lockstep with tests-unit.yml / tests-integration.yml and with the
# dagger-io pin the workflow installs.
DAGGER_VERSION = "0.21.7"

# Install the pinned SDK, then run the filing script. Nothing else belongs in
# the container.
PIPELINE_EXEC_COUNT = 2


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
    the workflow-run-target-code-checkout weakness semgrep flags.
    """
    checkouts = [s for s in steps if "actions/checkout" in str(s.get("uses", ""))]
    assert checkouts, "expected a checkout step"
    for step in checkouts:
        with_block = step.get("with") or {}
        assert "ref" not in with_block, (
            "must check out the default branch, not the failing head"
        )
        assert with_block.get("persist-credentials") is False


def test_agent_step_holds_no_credential_that_can_change_anything(
    steps: list[dict[str, Any]],
) -> None:
    """The agent may hold WARP_API_KEY (it is the thing being called) but must never
    hold a Linear key or a GitHub token -- that is what keeps a prompt-injected
    diagnosis from acting on either system.
    """
    agent = _step(steps, "Diagnose with Oz")
    env = agent.get("env") or {}
    assert "LINEAR_API_KEY" not in env, "the agent must not be able to write Linear"
    assert "GH_TOKEN" not in env, "the agent must not be able to act on GitHub"
    assert "WARP_API_KEY" in env, "the agent call needs its own key"


def test_no_uses_action_runs_pipeline_logic(steps: list[dict[str, Any]]) -> None:
    """Fully containerized: `uses:` may install toolchains, never run logic.

    An agent invoked via `uses:` cannot run inside a container, which is the whole
    reason this moved to oz-agent-sdk. Checkout, the Dagger CLI and `uv` are all
    provisioning steps -- they put a binary on PATH and decide nothing.
    """
    allowed = ("actions/checkout", "dagger/dagger-for-github", "astral-sh/setup-uv")
    for step in steps:
        uses = str(step.get("uses", ""))
        if not uses:
            continue
        assert uses.startswith(allowed), f"unexpected action step: {uses}"
    assert not any("oz-agent-action" in str(step.get("uses", "")) for step in steps), (
        "the agent must be invoked through the SDK in a container, not the action"
    )


def test_agent_context_is_extracted_on_the_runner(
    steps: list[dict[str, Any]],
) -> None:
    """A cloud agent cannot read our checkout, so the diff must be extracted here.

    Without this the agent would have neither the failing tree nor its diff, and a
    log-only diagnosis is materially weaker.
    """
    extract = _step(steps, "Extract the diff")
    assert "git diff origin/HEAD" in extract["run"]
    diagnose = _step(steps, "Diagnose with Oz")
    assert "--diff-file" in diagnose["run"]
    assert "--log-file" in diagnose["run"]


def test_reporting_does_not_depend_on_the_agent(steps: list[dict[str, Any]]) -> None:
    """13 silent red nights is the motivating failure; a broken agent must not
    reproduce it. A log-only stub is filed when no diagnosis comes back.
    """
    diagnose = _step(steps, "Diagnose with Oz")
    assert diagnose.get("continue-on-error") is True, (
        "a failed agent must not fail the job"
    )
    ensure = _step(steps, "Ensure a diagnosis exists")
    assert "tmp/diagnosis.md" in ensure["run"]
    assert ensure.get("if") is not None


def test_no_github_context_interpolated_into_shell(steps: list[dict[str, Any]]) -> None:
    """Run metadata is attacker-influencable and `${{ }}` is substituted before the
    shell parses it (semgrep run-shell-injection). It must arrive via `env:`.
    """
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
    for name, module in (
        ("Diagnose with Oz (Dagger)", "triage_diagnose_dagger.py"),
        ("File or bump the Linear issue (Dagger)", "triage_dagger.py"),
    ):
        step = _step(steps, name)
        assert "dagger run python" in step["run"], name
        assert f".github/workflows/ci/{module}" in step["run"], name


def test_host_fallback_exists_and_only_runs_when_dagger_failed(
    steps: list[dict[str, Any]],
) -> None:
    """registry.dagger.io brownouts (dagger/dagger#7548) already force six retries
    in tests-integration.yml. A reporter that dies with the registry is useless
    exactly when CI is unhappy.
    """
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
        if name.startswith(
            ("Diagnose", "File or bump", "Setup Dagger", "Extract the diff"),
        ):
            assert "steps.preflight.outputs.configured" in str(step.get("if", "")), (
                f"{name} must be gated on the preflight"
            )


def test_pipeline_mounts_only_the_script_and_its_adapter() -> None:
    """A broken lockfile or poisoned tree must not influence the filing step --
    this pipeline runs precisely because something in CI is already broken.

    The filing script reaches Linear through ``libs.linear`` rather than raw
    GraphQL, so the container needs one pinned wheel and the adapter package.
    What it must still never acquire is a route to this repo's *dependency
    graph*: `uv sync` is itself a plausible cause of the failure being triaged.
    """
    source = PIPELINE.read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]
    assert 'dag.host().directory("scripts", include=[SCRIPT_NAME])' in source
    assert 'dag.host().directory("libs", include=["__init__.py", "linear/**"])' in (
        source
    ), "mount the adapter, not the whole libs tree"
    assert "with_secret_variable" in source, "the key must not be an image layer"
    assert "dag.host().file(str(CONSTRAINTS_FILE))" in source, (
        "the hash-locked closure must be mounted, not resolved from PyPI directly"
    )

    # Two execs: the dependency install, then the script. The install comes
    # first so its layer caches on the base image and the pin alone -- a
    # diagnosis file that changes every run must not re-resolve the wheel.
    execs = [line for line in source.splitlines() if ".with_exec(" in line]
    assert len(execs) == PIPELINE_EXEC_COUNT, f"expected two with_exec, found {execs}"
    assert "sh" in execs[1], "the exec wraps the script so its exit code is captured"

    install_block = source.split(".with_exec(", 2)[1]
    assert '"pip"' in install_block
    assert "--require-hashes" in install_block, (
        "the closure, not just the top-level pin, must be hash-verified"
    )
    assert "CONSTRAINTS_IN_CONTAINER" in install_block

    for forbidden in ("uv sync", "apt-get", "uv.lock"):
        assert forbidden not in body, (
            f"{forbidden!r} would couple filing to the repo's dependency graph"
        )


def test_gtm_linear_pin_agrees_everywhere_it_appears() -> None:
    """The container pin, the script's PEP 723 pin, the project floor, and the
    hash-locked constraints file.

    Drift is invisible until a Linear failure in CI, and then the Dagger path
    and the host fallback would file through different SDK versions.
    """
    pin = re.search(
        r'GTM_LINEAR_PIN = "gtm-linear==([^"]+)"',
        PIPELINE.read_text(encoding="utf-8"),
    )
    assert pin is not None, "the pipeline must pin gtm-linear"
    version = pin.group(1)

    script = FILING_SCRIPT.read_text(encoding="utf-8")
    pep_723_header = script.split("# ///", 2)[1]
    assert f'"gtm-linear=={version}"' in pep_723_header, (
        "the PEP 723 header drives the host fallback; keep it on the container pin"
    )

    floor = re.search(
        r'"gtm-linear>=([^"]+)"',
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    assert floor is not None
    assert floor.group(1) == version, "the project floor and the CI pin must not drift"

    constraints = GTM_LINEAR_CONSTRAINTS.read_text(encoding="utf-8")
    assert f"gtm-linear=={version} \\" in constraints, (
        "the constraints file the container installs with --require-hashes must "
        "pin the same version"
    )


def test_oz_sdk_pin_agrees_everywhere_it_appears() -> None:
    """The diagnose container's pin, its script's PEP 723 pin, and its
    hash-locked constraints file.

    Mirrors test_gtm_linear_pin_agrees_everywhere_it_appears -- fixing drift
    detection for one triage pipeline and not the other is half a fix.
    """
    pin = re.search(
        r'OZ_SDK_PIN = "oz-agent-sdk==([^"]+)"',
        DIAGNOSE_PIPELINE.read_text(encoding="utf-8"),
    )
    assert pin is not None, "the pipeline must pin oz-agent-sdk"
    version = pin.group(1)

    script = DIAGNOSE_SCRIPT.read_text(encoding="utf-8")
    assert f'dependencies = ["oz-agent-sdk=={version}"' in script, (
        "the PEP 723 header drives the host fallback; keep it on the container pin"
    )

    constraints = OZ_SDK_CONSTRAINTS.read_text(encoding="utf-8")
    assert f"oz-agent-sdk=={version} \\" in constraints, (
        "the constraints file the container installs with --require-hashes must "
        "pin the same version"
    )


@pytest.mark.parametrize(
    "constraints_file",
    [GTM_LINEAR_CONSTRAINTS, OZ_SDK_CONSTRAINTS],
    ids=["gtm-linear", "oz-agent-sdk"],
)
def test_constraints_file_is_fully_hash_locked(constraints_file: Path) -> None:
    """Every requirement line carries a hash, not just the top-level pin.

    Catches an accidental regen without `--generate-hashes` -- `pip install
    --require-hashes` would then refuse everything, but silently, and only
    inside the container.
    """
    lines = constraints_file.read_text(encoding="utf-8").splitlines()
    requirement_indices = [
        i for i, line in enumerate(lines) if re.match(r"^[A-Za-z0-9._-]+==", line)
    ]
    assert requirement_indices, f"{constraints_file} has no pinned requirements"
    for i in requirement_indices:
        assert "--hash=sha256:" in lines[i + 1], (
            f"{lines[i]!r} in {constraints_file} has no hash on the following line"
        )


def test_script_lockfiles_exist_for_the_host_fallback() -> None:
    """`uv run --script` picks up a sibling `.lock` file automatically.

    Without it, a host-fallback run resolves the PEP 723 closure fresh from
    PyPI instead of against a hash-verified lock.
    """
    for lockfile in (FILING_SCRIPT_LOCK, DIAGNOSE_SCRIPT_LOCK):
        assert lockfile.is_file(), f"missing script lockfile: {lockfile}"


def test_uv_is_installed_before_the_host_fallback(
    steps: list[dict[str, Any]],
) -> None:
    """The fallback re-execs through `uv run --script` to honour its PEP 723 pin.

    Without `uv` on PATH the bootstrap fails closed, so the fallback files
    nothing -- and it only ever runs when the Dagger path has already failed.
    """
    setup = _step(steps, "Install uv")
    assert str(setup["uses"]).startswith("astral-sh/setup-uv@")

    names = [str(step.get("name", "")) for step in steps]
    assert names.index(str(setup["name"])) < names.index(
        "File or bump the Linear issue (host fallback)",
    )
