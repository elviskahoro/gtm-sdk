"""Static invariants for the Namespace-backed Unit-test workflow.

These tests validate the workflow changes from issues #296 and #312:
- Namespace-native checkout and caching actions
- Auto-provisioned (non-persistent) Dagger engine with image preflight
- Diagnostic output for cache behavior measurement
- No regression from previous setup
"""

from pathlib import Path


WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "tests-unit.yml"
PYTEST_DAGGER = (
    Path(__file__).parents[2] / ".github" / "workflows" / "ci" / "pytest_dagger.py"
)
PYTEST_INTEGRATION_DAGGER = (
    Path(__file__).parents[2]
    / ".github"
    / "workflows"
    / "ci"
    / "pytest_integration_dagger.py"
)


def test_unit_workflow_uses_namespace_checkout_and_host_cache() -> None:
    workflow = WORKFLOW.read_text()

    assert (
        "namespacelabs/nscloud-checkout-action@445c25d7009680597d73eb03c4e1cd5be522ed73 "
        "# v9.0.0"
    ) in workflow
    assert "fetch-depth: 0" in workflow
    assert (
        "namespacelabs/nscloud-cache-action@58bf6e08898e88803c098e2b522668541cd3b2e3 "
        "# v1.6.0"
    ) in workflow
    assert "~/.dagger-venv" in workflow
    assert "cache: uv" in workflow
    assert "UV_PYTHON_INSTALL_DIR" in workflow
    assert "steps.namespace_cache.outputs.cache-hit" in workflow


def test_unit_workflow_installs_uv_before_namespace_uv_cache() -> None:
    # `cache: uv` planning execs `uv cache dir` (spacectl), so setup-uv must
    # already be on PATH — wrong order kills the job before any test runs
    # (run 29462473211: `exec: "uv": executable file not found in $PATH`).
    workflow = WORKFLOW.read_text()
    assert workflow.index("astral-sh/setup-uv@") < workflow.index(
        "namespacelabs/nscloud-cache-action@",
    )
    # setup-uv's own GitHub-cache layer stays off; the Namespace cache action
    # is the sole owner of uv's cache dir and toolchains.
    assert "enable-cache: false" in workflow


def test_unit_workflow_preserves_dagger_caches_and_fallbacks() -> None:
    workflow = WORKFLOW.read_text()
    dagger = PYTEST_DAGGER.read_text()

    assert "runs-on: namespace-profile-test" in workflow
    assert "version: 0.21.7" in workflow
    assert "NSC_CACHE_PATH" in workflow
    assert "timeout 150 docker pull" in workflow
    assert "for attempt in $(seq 1 6)" in workflow
    assert "DAGGER_CLOUD_TOKEN: ${{ secrets.DAGGER_CLOUD_TOKEN }}" in workflow
    assert "dagger run python .github/workflows/ci/pytest_dagger.py" in workflow
    assert "trunk-io/analytics-uploader@" in workflow
    assert "junit-paths: junit.xml" in workflow
    assert '"uv-cache"' in dagger
    assert '"venv"' in dagger
    assert '.with_mounted_cache("/root/.cache/uv", uv_cache)' in dagger
    assert '.with_mounted_cache("/src/.venv", venv_cache)' in dagger
    assert "--junit-xml=junit.xml" in dagger
    assert "echo $? > /src/pytest_rc" in dagger
    assert "sys.exit(rc)" in dagger
    assert 'await ctr.file("/src/junit.xml").export(JUNIT_HOST_PATH)' in dagger
    assert "actions/cache" not in workflow
    assert "buildx" not in workflow.lower()
    assert "qemu" not in workflow.lower()
    assert "cache-from" not in workflow
    assert "cache-to" not in workflow


def test_unit_workflow_supports_manual_dispatch() -> None:
    # Measurement work (issues #303/#305) needs sequential runs on demand;
    # marker commits pollute history and roborev/PR flows.
    workflow = WORKFLOW.read_text()
    assert "workflow_dispatch:" in workflow


def test_unit_workflow_has_no_persistent_engine_layer() -> None:
    # The persistent Dagger engine (#290) was rolled back after the #312 A/B:
    # with the #305/#310 telemetry exit-stalls fixed, its fixed cost per run
    # (4s start + 20-25s awaited graceful stop; the upload overlap reclaimed
    # only ~3s of that) exceeded its ~5s warm-pytest benefit. The CLI
    # auto-provisions a fresh engine per run; the image preflight and the
    # cache-volume prune below are what remain of the engine layer.
    workflow = WORKFLOW.read_text()

    assert "- name: Start Dagger engine with persistent state" not in workflow
    assert "- name: Report Dagger engine state" not in workflow
    assert "- name: Start graceful Dagger engine shutdown" not in workflow
    assert "- name: Await graceful Dagger engine shutdown" not in workflow
    assert "_EXPERIMENTAL_DAGGER_RUNNER_HOST" not in workflow
    assert "docker stop" not in workflow

    assert "- name: Prune stale Dagger engine state" in workflow
    assert 'sudo rm -rf "${NSC_CACHE_PATH}/dagger-engine"' in workflow


def test_unit_workflow_dagger_venv_survives_cache_mount() -> None:
    # `uv venv --clear` deletes and recreates ~/.dagger-venv, detaching it
    # from the nscloud-cache-action mount — the venv then never persists
    # (observed cold in 5/5 runs, issue #303). The venv must be created into
    # the existing directory, and a cache-restored venv must be validated by
    # importing the SDK, not by an -x file test.
    workflow = WORKFLOW.read_text()
    assert 'uv venv "$HOME/.dagger-venv" --clear' not in workflow
    assert "UV_VENV_CLEAR" not in workflow
    assert 'find "$HOME/.dagger-venv" -mindepth 1 -delete' in workflow
    assert "import dagger, anyio" in workflow


def test_dagger_pipelines_export_exit_codes_without_contents_readback() -> None:
    for pipeline in (PYTEST_DAGGER, PYTEST_INTEGRATION_DAGGER):
        source = pipeline.read_text()
        assert ".contents()" not in source
        assert "file(PYTEST_RC_PATH).export(PYTEST_RC_HOST_PATH)" in source
        assert 'file("/src/junit.xml").export(JUNIT_HOST_PATH)' in source
        assert '"pytest_rc",' in source
        assert "sys.exit(rc)" in source


def test_unit_dagger_pipeline_uses_measured_two_cpu_configuration() -> None:
    source = PYTEST_DAGGER.read_text()

    assert "uv run pytest --junit-xml=junit.xml" in source
    assert "-n auto" not in source
    assert "--dist=loadfile" not in source
    assert "Dagger pipeline evaluation + pytest_rc export:" in source
    assert "Dagger transfer pytest_rc:" not in source
