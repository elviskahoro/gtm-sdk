"""Static invariants for the Namespace-backed Unit-test workflow.

These tests validate the workflow changes from issues #296 and #321:
- Namespace-native checkout and caching actions
- Fresh Dagger engines with host-seeded uv caches
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
    assert "~/.dagger-sdk" in workflow
    assert "cache: uv" in workflow
    assert "UV_PYTHON_INSTALL_DIR" in workflow
    assert "steps.namespace_cache.outputs.cache-hit" in workflow
    # Toolchain + venv are siblings under one Namespace mount — never target
    # `uv venv` at the mount root (detaches the bind, issue #303).
    assert '"$HOME/.dagger-sdk/uv-python"' in workflow
    assert 'dagger_venv="$HOME/.dagger-sdk/venv"' in workflow
    cache_paths = workflow.split("path: |", 1)[1].split("- name:", 1)[0]
    assert "~/.dagger-sdk" in cache_paths
    assert "~/.dagger-venv" not in cache_paths
    assert "local/share/uv/python" not in cache_paths


def test_unit_workflow_installs_uv_before_namespace_uv_cache() -> None:
    # `cache: uv` planning execs `uv cache dir`, so setup-uv must already be on
    # PATH before the Namespace cache action runs.
    workflow = WORKFLOW.read_text()
    assert workflow.index("astral-sh/setup-uv@") < workflow.index(
        "namespacelabs/nscloud-cache-action@",
    )
    # setup-uv's own GitHub-cache layer stays off; Namespace owns uv artifacts
    # and the Dagger SDK/toolchain.
    assert "enable-cache: false" in workflow


def test_unit_workflow_seeds_dagger_uv_cache_and_uses_fallbacks() -> None:
    workflow = WORKFLOW.read_text()
    dagger = PYTEST_DAGGER.read_text()

    assert "runs-on: namespace-profile-test" in workflow
    assert "version: 0.21.7" in workflow
    assert "/var/lib/dagger" not in workflow
    assert "_EXPERIMENTAL_DAGGER_RUNNER_HOST" not in workflow
    assert "docker-container://" not in workflow
    assert "docker stop" not in workflow
    assert "timeout 150 docker pull" in workflow
    assert "for attempt in $(seq 1 6)" in workflow
    assert "DAGGER_CLOUD_TOKEN: ${{ secrets.DAGGER_CLOUD_TOKEN }}" in workflow
    assert "dagger run python .github/workflows/ci/pytest_dagger.py" in workflow
    assert "trunk-io/analytics-uploader@" in workflow
    assert "junit-paths: junit.xml" in workflow
    assert '"uv-cache"' in dagger
    assert '"venv"' in dagger
    assert 'dag.host().directory(str(Path.home() / ".cache" / "uv"))' in dagger
    assert "Dagger uv cache: seeding from Namespace host cache" in dagger
    assert (
        '.with_mounted_cache(\n            "/root/.cache/uv",\n            uv_cache,\n            source=host_uv_cache,\n        )'
        in dagger
    )
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


def test_unit_workflow_warms_project_uv_cache_on_host() -> None:
    workflow = WORKFLOW.read_text()

    assert "Warm project uv cache" in workflow
    assert 'project_env="$HOME/.dagger-sdk/project-venv"' in workflow
    assert 'cache_key_file="${project_env}/.gtm-sdk-cache-key"' in workflow
    assert "sha256sum pyproject.toml uv.lock" in workflow
    assert 'UV_PROJECT_ENVIRONMENT="${project_env}" uv sync' in workflow
    assert "--all-extras --dev --locked" in workflow
    assert 'rm -rf "${project_env}"' in workflow
    assert 'printf \'%s\\n\' "${cache_key}" >"${cache_key_file}"' in workflow
    assert "cache: uv" in workflow
    assert "~/.cache/uv" in workflow


def test_unit_workflow_supports_manual_dispatch() -> None:
    # Measurement work (issues #303/#305) needs sequential runs on demand;
    # marker commits pollute history and roborev/PR flows.
    workflow = WORKFLOW.read_text()
    assert "workflow_dispatch:" in workflow


def test_unit_workflow_uses_a_fresh_dagger_engine() -> None:
    workflow = WORKFLOW.read_text()

    assert "- name: Start Dagger engine with persistent state" not in workflow
    assert "- name: Report Dagger engine state" not in workflow
    assert "- name: Start graceful Dagger engine shutdown" not in workflow
    assert "- name: Await graceful Dagger engine shutdown" not in workflow
    assert "_EXPERIMENTAL_DAGGER_RUNNER_HOST" not in workflow
    assert ":/var/lib/dagger" not in workflow
    assert "docker stop" not in workflow


def test_unit_workflow_dagger_venv_survives_cache_mount() -> None:
    # `uv venv --clear` on the Namespace mount root deletes and recreates the
    # directory, detaching the bind — the tree then never persists (cold in
    # 5/5 runs, issue #303). Mount ~/.dagger-sdk; put the venv at a
    # subdirectory so cold recreate can replace it; keep the toolchain as a
    # sibling under the same mount. Validate restores by importing the SDK.
    workflow = WORKFLOW.read_text()
    assert 'uv venv "$HOME/.dagger-sdk" --clear' not in workflow
    assert 'uv venv "$HOME/.dagger-venv" --clear' not in workflow
    assert "UV_VENV_CLEAR" not in workflow
    assert 'rm -rf "${dagger_venv}" "${UV_PYTHON_INSTALL_DIR}"' in workflow
    assert "validation error: ${err}" in workflow
    assert "import dagger, anyio" in workflow
    assert 'uv venv --python 3.13 "${dagger_venv}"' in workflow
    assert "uv python install --reinstall 3.13" in workflow
    assert "interpreter escaped ~/.dagger-sdk" in workflow
    assert "uv Python toolchain is sibling under ~/.dagger-sdk" in workflow


def test_dagger_pipelines_export_exit_codes_without_contents_readback() -> None:
    for pipeline in (PYTEST_DAGGER, PYTEST_INTEGRATION_DAGGER):
        source = pipeline.read_text()
        assert ".contents()" not in source
        assert "file(PYTEST_RC_PATH).export(PYTEST_RC_HOST_PATH)" in source
        assert 'file("/src/junit.xml").export(JUNIT_HOST_PATH)' in source
        assert '"pytest_rc",' in source
        assert "sys.exit(rc)" in source


def test_unit_dagger_pipeline_uses_measured_four_worker_configuration() -> None:
    source = PYTEST_DAGGER.read_text()

    assert "uv run --no-sync pytest" in source
    assert "-n 4 --dist=loadfile" in source
    assert "-p xdist.plugin" in source
    assert "-p pytest_asyncio.plugin" in source
    assert "-p anyio.pytest_plugin" in source
    assert '"PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"' in source
    assert "--no-install-project" not in source
    assert '"--all-extras",' in source
    assert '"--locked",' in source
    for excluded in ('".git"', '".entire"', '".kilo"'):
        assert excluded in source
    assert "--junit-xml=junit.xml" in source
    assert "echo $? > /src/pytest_rc" in source
    assert 'await ctr.file("/src/junit.xml").export(JUNIT_HOST_PATH)' in source
    assert "Dagger pipeline evaluation + pytest_rc export:" in source
    assert "Dagger transfer pytest_rc:" not in source
