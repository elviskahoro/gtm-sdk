# ruff: noqa: S101 -- asserts are the point of a workflow contract test.

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

import ast
import re
import tomllib
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


def test_pipeline_uses_the_locked_full_dependency_image() -> None:
    source = PYTEST_INTEGRATION_DAGGER.read_text(encoding="utf-8")
    dependency_image = (
        PYTEST_INTEGRATION_DAGGER.parent / "pytest_dependency_image.py"
    ).read_text(encoding="utf-8")

    assert "from pytest_dependency_image import" in source
    assert "dependency_base(source)" in source
    assert "curl -LsSf https://astral.sh/uv/install.sh" not in source
    assert '"uv", "sync"' not in source
    assert "uv sync --all-extras --dev --compile-bytecode --locked" in dependency_image

    manifest = tomllib.loads(
        (Path(__file__).parents[2] / ".flox" / "env" / "manifest.toml").read_text(
            encoding="utf-8",
        ),
    )
    assert manifest["install"]["uv"]["version"] == "0.11.26"
    assert '.with_directory("/workspace/.flox", source.directory(".flox"))' in (
        dependency_image
    )


def test_integration_secrets_are_content_addressed() -> None:
    source = PYTEST_INTEGRATION_DAGGER.read_text(encoding="utf-8")

    assert "sha256(value.encode()).hexdigest()" in source
    assert "dag.set_secret(secret_name, value)" in source


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


def test_runs_on_a_github_hosted_arm64_runner(steps: list[dict[str, Any]]) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    assert workflow["jobs"]["pytest-integration"]["runs-on"] == "ubuntu-24.04-arm"
    checkout = _step(steps, "Checkout")
    assert checkout["uses"].startswith("actions/checkout@")
    cache = _step(steps, "Restore Dagger Python SDK")
    assert cache["uses"].startswith("actions/cache/restore@")
    assert "runner.arch" in cache["with"]["key"]


def test_dagger_sdk_install_validates_the_cached_venv(
    steps: list[dict[str, Any]],
) -> None:
    install = _step(steps, "Install Dagger Python SDK")
    run = install["run"]

    assert "uv venv" in run
    assert "uv pip install --python" in run
    assert "$GITHUB_PATH" in run
    assert 'version("dagger-io") == "0.21.7"' in run


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


def test_every_dagger_base_image_is_digest_pinned() -> None:
    ci_dir = PYTEST_INTEGRATION_DAGGER.parent

    for path in sorted(ci_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assignments = {
            target.id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "from_"
                and node.args
            ):
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Name):
                argument = assignments.get(argument.id)
            rendered = ast.unparse(argument) if argument is not None else ""
            try:
                image = ast.literal_eval(argument) if argument is not None else None
            except (ValueError, SyntaxError):
                image = None
            assert isinstance(image, str)
            assert re.search(
                r"@sha256:[0-9a-fA-F]{64}$",
                image,
            ), f"{path}:{node.lineno} uses an unpinned Dagger base: {rendered}"
