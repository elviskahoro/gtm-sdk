"""Static invariants for the nightly Integration-test workflow.

Guards the regressions from issue #223, which cost ten days of silently red
nightly runs:

- `--cloud` must stay out of the executed steps. The elviskahoro org cannot mint
  a Dagger Cloud `Engine`-type token without Cloud Engines early access, so it
  dies at engine provisioning before pytest starts. #222 adopted it anyway.

  This guard is a speed bump, not a verdict. Dagger has a managed-compute
  product landing and #223 stays open to revisit `--cloud` when it ships;
  updating this test is the expected first step of that refactor. Dispatch
  `dagger-cloud-smoke.yml` to confirm the entitlement before starting.
- `DAGGER_CLOUD_COMPUTE_TOKEN` must stay out. It was a manually-set repo secret
  that got deleted in a secret re-sync, and GitHub expands a missing secret to
  "" rather than failing, so the job kept running against no credential at all.
- The #221 engine-pull mitigation must stay in. #222 deleted it as redundant
  under `--cloud`; reverting `--cloud` without restoring it would leave this job
  exposed to the registry.dagger.io brownouts (dagger/dagger#7548) that motivated
  the whole exercise.

These parse the workflow rather than substring-matching it, so commenting a step
out fails the guard instead of silently satisfying it.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml


WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "tests-integration.yml"
PYTEST_INTEGRATION_DAGGER = (
    Path(__file__).parents[2]
    / ".github"
    / "workflows"
    / "ci"
    / "pytest_integration_dagger.py"
)

PIPELINE_PATH = ".github/workflows/ci/pytest_integration_dagger.py"

# Read at runtime by the integration suite; forwarded into the container as
# Dagger secrets by the pipeline. Kept in sync with INTEGRATION_SECRET_ENV_VARS
# in pytest_integration_dagger.py, which the credential test cross-checks.
SUITE_CREDENTIALS = (
    "ATTIO_API_KEY",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "PARALLEL_API_KEY",
)


@pytest.fixture
def steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    jobs = workflow["jobs"]
    assert len(jobs) == 1, "expected a single job; update these guards if that changes"
    return list(jobs["pytest-integration"]["steps"])


@pytest.fixture
def run_step(steps: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [step for step in steps if PIPELINE_PATH in step.get("run", "")]
    assert len(matches) == 1, f"expected exactly one step invoking {PIPELINE_PATH}"
    return matches[0]


def _step(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


def test_pipeline_runs_on_a_local_dagger_engine(run_step: dict[str, Any]) -> None:
    assert run_step["run"].strip() == f"dagger run python {PIPELINE_PATH}"


def test_no_step_reaches_for_the_dagger_cloud_engine(
    steps: list[dict[str, Any]],
) -> None:
    for step in steps:
        rendered = yaml.safe_dump(step)
        assert "--cloud" not in rendered, (
            f"step {step.get('name')!r} still uses --cloud"
        )
        assert "DAGGER_CLOUD_COMPUTE_TOKEN" not in rendered, (
            f"step {step.get('name')!r} references the deleted compute-token secret"
        )


def test_runs_on_a_namespace_runner(steps: list[dict[str, Any]]) -> None:
    """The profile's container-image cache is why this job is on Namespace.

    It serves registry.dagger.io/engine locally after the first run — the same
    property `--cloud` was chasing. Dropping back to a GitHub-hosted runner would
    silently reinstate the cold pull on every run.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text())

    assert workflow["jobs"]["pytest-integration"]["runs-on"] == "namespace-profile-test"
    checkout = _step(steps, "Checkout")
    assert checkout["uses"].startswith("namespacelabs/nscloud-checkout-action@")


def test_dagger_sdk_install_survives_pep_668(steps: list[dict[str, Any]]) -> None:
    """No bare `pip`/`--system` install — the Namespace image is externally managed.

    Its system Python is not writable without root, so `pip install dagger-io
    anyio` (what this job used on ubuntu-latest) fails outright. The venv's bin
    must reach $GITHUB_PATH or `dagger run python` resolves an interpreter with
    no SDK.
    """
    install = _step(steps, "Install Dagger Python SDK")
    run = install["run"]

    assert "uv venv" in run
    assert "uv pip install --python" in run
    assert "$GITHUB_PATH" in run
    assert "pip install dagger-io" not in run
    assert "--system" not in run


def test_engine_pull_mitigation_is_present(steps: list[dict[str, Any]]) -> None:
    resolve = _step(steps, "Resolve Dagger engine version")
    assert resolve["id"] == "dagger_engine"

    pull = _step(steps, "Ensure Dagger engine image (resilient to registry flakiness)")
    # Local-daemon hit first: on Namespace the profile's container-image cache
    # makes this the common path, so the registry is never touched.
    assert "docker image inspect" in pull["run"]
    # Bounded retry with a per-attempt timeout: a hung blob must cede to a fresh
    # attempt rather than wedging the job (dagger/dagger#7548).
    assert "for attempt in $(seq 1 6)" in pull["run"]
    assert "timeout 150 docker pull" in pull["run"]


def test_dagger_cli_and_sdk_versions_are_pinned_together(
    steps: list[dict[str, Any]],
) -> None:
    """An unpinned CLI silently changes the engine version and forces cold pulls.

    The SDK pin must track it — a client/engine skew is the kind of thing that
    only shows up as a confusing runtime error inside the pipeline.
    """
    setup = _step(steps, "Setup Dagger")
    install = _step(steps, "Install Dagger Python SDK")

    cli_version = str(setup["with"]["version"])
    assert f'"dagger-io=={cli_version}"' in install["run"]


def test_run_step_forwards_every_suite_credential(run_step: dict[str, Any]) -> None:
    """Credentials must be on the step that actually runs the pipeline.

    Anything missing surfaces as a collection-time preflight failure inside the
    container rather than an obvious workflow config error.
    """
    pipeline = PYTEST_INTEGRATION_DAGGER.read_text()
    env = run_step["env"]

    for secret in SUITE_CREDENTIALS:
        assert f'"{secret}"' in pipeline, f"{secret} is no longer read by the pipeline"
        assert env[secret] == f"${{{{ secrets.{secret} }}}}"


def test_dagger_cloud_token_is_telemetry_only(run_step: dict[str, Any]) -> None:
    """Present for traces, and deliberately not gated by a preflight.

    This token type is valid for telemetry; only engine provisioning ever
    rejected it. Absence degrades to "no traces", not a failed run.
    """
    assert run_step["env"]["DAGGER_CLOUD_TOKEN"] == "${{ secrets.DAGGER_CLOUD_TOKEN }}"
