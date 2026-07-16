"""Static invariants for the Namespace-backed Unit-test workflow.

These tests validate the workflow changes from issues #296, #321, and #330:
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
    # One invocation owns the native uv cache, project venv, and uv-managed
    # toolchain. Correctness comes from inspecting the restored venv (#330),
    # never from a separately persisted sidecar fingerprint.
    assert workflow.count("namespacelabs/nscloud-cache-action@") == 1
    assert "gtm-sdk-cache" not in workflow
    assert "placeholder" not in workflow
    assert "cache_key_file" not in workflow
    assert "fingerprint" not in workflow.lower()
    assert "writeback scope" not in workflow.lower()
    assert "UV_PYTHON_INSTALL_DIR" in workflow
    assert "steps.namespace_cache.outputs.cache-hit" in workflow
    # Toolchain + venv are siblings under one Namespace mount — never target
    # `uv venv` at the mount root (detaches the bind, issue #303).
    assert '"$HOME/.dagger-sdk/uv-python"' in workflow
    assert 'dagger_venv="$HOME/.dagger-sdk/venv"' in workflow
    cache_paths = workflow.split("path: |", 1)[1].split("- name:", 1)[0]
    assert "~/.dagger-sdk/venv" in cache_paths
    assert "~/.dagger-sdk/uv-python" in cache_paths
    assert "~/gtm-sdk-cache" not in cache_paths
    assert "~/.dagger-venv" not in cache_paths
    assert "local/share/uv/python" not in cache_paths


def test_unit_workflow_reports_mounted_cache_diagnostics() -> None:
    # A stale volume fork must be diagnosable from logs alone (#330): capture
    # mount state before any step mutates it.
    workflow = WORKFLOW.read_text()

    assert "Report mounted cache diagnostics" in workflow
    assert 'uv_cache_dir="$(uv cache dir)"' in workflow
    assert 'findmnt -R "${uv_cache_dir}"' in workflow
    assert 'findmnt -R "$HOME/.dagger-sdk/venv"' in workflow
    assert 'findmnt -R "$HOME/.dagger-sdk/uv-python"' in workflow
    assert 'ls -la "${NSC_CACHE_PATH}"' in workflow
    assert "Fingerprint at mount time" not in workflow
    assert "Mounted metadata directory contents" not in workflow
    # Diagnostics run after the mount and before the SDK install can mutate
    # the venv.
    assert workflow.index("Cache host Dagger and uv data") < workflow.index(
        "Report mounted cache diagnostics",
    )
    assert workflow.index("Report mounted cache diagnostics") < workflow.index(
        "Install Dagger Python SDK",
    )


def test_unit_workflow_validates_the_restored_environment_before_syncing() -> None:
    # Namespace may restore a stale generation, so only uv's read-only check of
    # the actual environment can decide whether dependency sync is necessary.
    workflow = WORKFLOW.read_text()
    warm_step = workflow.split("- name: Warm project uv cache", 1)[1].split(
        "- name: Report Namespace cache state",
        1,
    )[0]
    normalized = " ".join(warm_step.replace("\\\n", " ").split())
    check_command = (
        'UV_PROJECT_ENVIRONMENT="${project_env}" uv sync '
        "--all-extras --dev --locked --no-install-project --inexact --check "
        '--python "${project_env}/bin/python"'
    )
    sync_command = (
        'UV_PROJECT_ENVIRONMENT="${project_env}" uv sync '
        "--all-extras --dev --locked --no-install-project --inexact "
        '--python "${project_env}/bin/python"'
    )

    assert "check_project_env()" in warm_step
    assert check_command in normalized
    assert sync_command in normalized
    assert warm_step.count("check_project_env") == 3
    assert warm_step.count("--check") == 1
    assert 'if validation_output="$(check_project_env 2>&1)"; then' in warm_step
    assert "printf '%s\\n' \"${validation_output}\"" in warm_step
    assert 'echo "Host project uv cache: exact hit (${environment_key})"' in warm_step
    assert (
        'echo "Host project uv cache: miss or stale; syncing dependencies '
        '(${environment_key})"'
    ) in warm_step
    # Failed-check output (uv's package delta) is printed before reconciliation,
    # and the helper is called again afterward as a mandatory post-sync check.
    exact_hit = normalized.index(
        'echo "Host project uv cache: exact hit (${environment_key})"',
    )
    miss_branch = normalized.index("else", exact_hit)
    delta_output = normalized.index(
        'echo "Host project uv cache validation delta:"',
        miss_branch,
    )
    delta_details = normalized.index(
        "printf '%s\\n' \"${validation_output}\"",
        delta_output,
    )
    dependency_sync = normalized.index(sync_command, miss_branch)
    post_sync_check = normalized.rindex("check_project_env")
    assert exact_hit < miss_branch < delta_output < delta_details < dependency_sync
    assert post_sync_check > dependency_sync
    assert "cache_key_file" not in warm_step
    assert "miss_reasons" not in warm_step
    assert "fingerprint" not in warm_step.lower()


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


def test_unit_workflow_initializes_empty_namespace_mounts_without_deleting_them() -> (
    None
):
    workflow = WORKFLOW.read_text()

    assert 'uv venv --clear --python 3.13 "${dagger_venv}"' in workflow
    assert 'rm -rf "${dagger_venv}" "${UV_PYTHON_INSTALL_DIR}"' not in workflow


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
    assert "DAGGER_CLOUD_TOKEN: ${{ secrets.DAGGER_CLOUD_COMPUTE_TOKEN }}" in workflow
    assert "dagger --cloud run python .github/workflows/ci/pytest_dagger.py" in workflow
    assert (
        "DAGGER_NO_NAG=1 dagger run python .github/workflows/ci/pytest_dagger.py"
        in workflow
    )
    assert "trunk-io/analytics-uploader@" in workflow
    assert "junit-paths: junit.xml" in workflow
    assert '"uv-cache"' in dagger
    assert "dag.host().directory(str(HOST_PROJECT_ENV))" in dagger
    assert "dag.host().directory(str(HOST_UV_PYTHON))" in dagger
    assert "dag.host().directory(str(HOST_UV_CACHE))" in dagger
    assert 'dag.host().directory("scripts")' in dagger
    assert 'with_directory("/opt/gtm-sdk/scripts", scripts)' in dagger
    assert "Dagger uv cache: seeding from Namespace host cache" in dagger
    assert (
        '.with_mounted_cache(\n            "/root/.cache/uv",\n            uv_cache,\n            source=host_uv_cache,\n        )'
        in dagger
    )
    assert (
        ".with_mounted_directory(\n"
        "            str(HOST_PROJECT_ENV),\n"
        "            host_project_env,\n"
        "            read_only=True,\n"
        "        )" in dagger
    )
    assert (
        ".with_mounted_directory(\n"
        "            str(HOST_UV_PYTHON),\n"
        "            host_uv_python,\n"
        "            read_only=True,\n"
        "        )" in dagger
    )
    assert (
        '.with_env_variable("UV_PROJECT_ENVIRONMENT", str(HOST_PROJECT_ENV))' in dagger
    )
    assert '.with_env_variable("PYTHONPATH", "/src")' in dagger
    assert (
        ".with_mounted_directory(\n"
        "            str(HOST_UV_CACHE),\n"
        "            host_uv_cache,\n"
        "            read_only=True,\n"
        "        )" in dagger
    )
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
    assert 'project_env="$HOME/.dagger-sdk/venv"' in workflow
    assert 'uv_cache_dir="$(uv cache dir)"' in workflow
    assert "sha256sum pyproject.toml uv.lock" in workflow
    assert "'python=3.13'" in workflow
    assert '"arch=$(uname -m)"' in workflow
    assert 'UV_PROJECT_ENVIRONMENT="${project_env}" uv sync' in workflow
    assert "--all-extras --dev --locked" in workflow
    assert "--no-install-project --inexact --check" in workflow
    assert "uv pip install --no-deps --reinstall" in workflow
    assert '--python "${project_env}/bin/python" .' in workflow
    assert "dagger-io==0.21.7" in workflow
    assert "anyio==4.13.0" in workflow
    assert 'if [ ! -x "${project_env}/bin/python" ]; then' in workflow
    assert (
        'echo "Host project uv cache interpreter: ${project_env}/bin/python"'
        in workflow
    )
    assert (
        'echo "Host project uv cache toolchain: ${UV_PYTHON_INSTALL_DIR}"' in workflow
    )
    assert (
        'echo "Host project uv cache environment key: ${environment_key}"' in workflow
    )
    assert "Host project uv cache stamp" not in workflow
    assert "expected fingerprint" not in workflow
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


def test_unit_dagger_pipeline_consumes_host_project_environment() -> None:
    # The host project venv is mounted at its original absolute path so its
    # interpreter symlink resolves through the sibling uv toolchain mount.
    # Dagger must not recreate a separate `/src/.venv` on every fresh engine.
    workflow = WORKFLOW.read_text()
    dagger = PYTEST_DAGGER.read_text()
    assert 'project_env="$HOME/.dagger-sdk/venv"' in workflow
    assert 'UV_PROJECT_ENVIRONMENT="${project_env}" uv sync' in workflow
    assert 'dag.cache_volume("venv")' not in dagger
    assert '"$UV_PROJECT_ENVIRONMENT/bin/python" -m pytest ' in dagger
    assert '"/src/.venv"' not in dagger


def test_unit_dagger_pipeline_mounts_the_namespace_project_venv() -> None:
    dagger = PYTEST_DAGGER.read_text()

    assert 'HOST_PROJECT_ENV = HOST_DAGGER_SDK / "venv"' in dagger
    assert '".dagger-sdk" / "project-venv"' not in dagger


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

    assert '"$UV_PROJECT_ENVIRONMENT/bin/python" -m pytest ' in source
    assert "/opt/gtm-sdk" in source
    assert "-n 4 --dist=loadfile" in source
    assert "-p xdist.plugin" in source
    assert "-p pytest_asyncio.plugin" in source
    assert "-p anyio.pytest_plugin" in source
    assert '"PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"' in source
    for excluded in ('".git"', '".entire"', '".kilo"'):
        assert excluded in source
    assert "--junit-xml=junit.xml" in source
    assert "echo $? > /src/pytest_rc" in source
    assert 'await ctr.file("/src/junit.xml").export(JUNIT_HOST_PATH)' in source
    assert "Dagger pipeline evaluation + pytest_rc export:" in source
    assert "Dagger transfer pytest_rc:" not in source
