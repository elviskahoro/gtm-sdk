"""Static invariants for the nightly Integration-test workflow.

Guards the regressions from issue #223, which cost ten days of silently red
nightly runs:

- `--cloud` must stay out of the executed steps. The elviskahoro org cannot mint
  a Dagger Cloud `Engine`-type token without Cloud Engines early access, so it
  dies at engine provisioning before pytest starts. #222 adopted it anyway. Do
  not re-add it without closing #223 first.
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


def test_engine_pull_mitigation_is_present(steps: list[dict[str, Any]]) -> None:
    resolve = _step(steps, "Resolve Dagger engine version")
    assert resolve["id"] == "dagger_engine"

    cache = _step(steps, "Cache Dagger engine image")
    assert cache["uses"].startswith("actions/cache@")
    # Asserting on the workflow's literal cache path; nothing is created here.
    # trunk-ignore(bandit/B108)
    assert cache["with"]["path"] == "/tmp/dagger-engine.tar"
    # Keyed on the engine version so an upstream CLI bump pays exactly one cold run.
    assert "steps.dagger_engine.outputs.version" in cache["with"]["key"]

    pull = _step(
        steps,
        "Load or pre-pull Dagger engine image (resilient to registry flakiness)",
    )
    # Bounded retry with a per-attempt timeout: a hung blob must cede to a fresh
    # attempt rather than wedging the job (dagger/dagger#7548).
    assert "for attempt in $(seq 1 6)" in pull["run"]
    assert "timeout 150 docker pull" in pull["run"]


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
