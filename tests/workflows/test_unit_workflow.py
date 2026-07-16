"""Static invariants for the Namespace-backed Unit-test workflow.

These tests validate the workflow changes from issue #296:
- Namespace-native checkout and caching actions
- Preserved Dagger engine state and cache volumes
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
    # Toolchain must live under the venv tree (one Namespace mount), not as a
    # sibling path that can miss independently of ~/.dagger-venv.
    assert '"$HOME/.dagger-venv/uv-python"' in workflow
    cache_paths = workflow.split("path: |", 1)[1].split("- name:", 1)[0]
    assert "~/.dagger-venv" in cache_paths
    assert "local/share/uv/python" not in cache_paths


def test_unit_workflow_installs_uv_before_namespace_uv_cache() -> None:
    # `cache: uv` planning execs `uv cache dir` (spacectl), so setup-uv must
    # already be on PATH — wrong order kills the job before any test runs
    # (run 29462473211: `exec: "uv": executable file not found in $PATH`).
    workflow = WORKFLOW.read_text()
    assert workflow.index("astral-sh/setup-uv@") < workflow.index(
        "namespacelabs/nscloud-cache-action@",
    )
    # setup-uv's own GitHub-cache layer stays off; the Namespace cache action
    # is the sole owner of uv's cache dir. The managed CPython toolchain is
    # nested under ~/.dagger-venv (also on that volume), not a separate path.
    assert "enable-cache: false" in workflow


def test_unit_workflow_preserves_dagger_caches_and_fallbacks() -> None:
    workflow = WORKFLOW.read_text()
    dagger = PYTEST_DAGGER.read_text()

    assert "runs-on: namespace-profile-test" in workflow
    assert "version: 0.21.7" in workflow
    assert "NSC_CACHE_PATH" in workflow
    assert '-v "${state_dir}:/var/lib/dagger"' in workflow
    assert "docker-container://${name}" in workflow
    assert "engine_name=${name}" in workflow
    assert "engine failed to start on cached state" in workflow
    assert "falling back to a cold auto-provisioned engine" in workflow
    assert "timeout 150 docker pull" in workflow
    assert "for attempt in $(seq 1 6)" in workflow
    assert "DAGGER_CLOUD_TOKEN: ${{ secrets.DAGGER_CLOUD_TOKEN }}" in workflow
    assert "dagger run python .github/workflows/ci/pytest_dagger.py" in workflow
    assert "trunk-io/analytics-uploader@" in workflow
    assert "junit-paths: junit.xml" in workflow
    assert 'docker stop -t 300 "${ENGINE_NAME}"' in workflow
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


def test_unit_workflow_overlaps_graceful_engine_stop_with_results_upload() -> None:
    workflow = WORKFLOW.read_text()

    run_tests = workflow.index("- name: Run pytest in Dagger")
    start_stop = workflow.index("- name: Start graceful Dagger engine shutdown")
    upload_results = workflow.index("- name: Upload Test Results to Trunk.io")
    await_stop = workflow.index("- name: Await graceful Dagger engine shutdown")

    assert run_tests < start_stop < upload_results < await_stop
    assert "nohup bash -c " in workflow
    assert 'docker stop -t 300 "${ENGINE_NAME}"' in workflow
    assert (
        "if: always() && steps.dagger_engine_start.outputs.engine_name != ''"
        in workflow
    )


def test_unit_workflow_checks_asynchronous_engine_shutdown() -> None:
    workflow = WORKFLOW.read_text()
    shutdown_workflow = workflow[
        workflow.index("- name: Start graceful Dagger engine shutdown") :
    ]

    assert (
        "DAGGER_STOP_STATUS_FILE: ${{ runner.temp }}/dagger-engine-stop.status"
        in workflow
    )
    assert "DAGGER_STOP_LOG_FILE: ${{ runner.temp }}/dagger-engine-stop.log" in workflow
    assert (
        "DAGGER_STOP_STARTED_FILE: ${{ runner.temp }}/dagger-engine-stop.started"
        in workflow
    )
    assert "for _ in $(seq 1 310)" in workflow
    assert "shutdown process exited before writing status" in workflow
    assert "timed out waiting for graceful engine shutdown" in workflow
    assert "graceful engine shutdown failed with exit code" in workflow
    assert "docker inspect -f '{{.State.Running}}'" in workflow
    assert "docker inspect -f '{{.State.ExitCode}}'" in workflow
    assert "engine exited non-gracefully with status" in workflow
    assert 'docker logs --timestamps "${ENGINE_NAME}"' in workflow
    assert "docker kill" not in shutdown_workflow
    assert 'docker rm -f "${ENGINE_NAME}"' not in shutdown_workflow


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
    # Nested toolchain makes the venv root non-empty before bin/ is written.
    assert 'uv venv --allow-existing --python 3.13 "$HOME/.dagger-venv"' in workflow
    assert "uv python install --reinstall 3.13" in workflow
    assert "interpreter escaped venv tree" in workflow
    assert "uv Python toolchain is nested under venv" in workflow


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
