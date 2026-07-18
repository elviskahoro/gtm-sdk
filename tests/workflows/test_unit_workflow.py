"""Static invariants for the Namespace-backed Unit-test workflow.

These tests validate the workflow changes from issues #296, #321, and #330:
- Namespace-native checkout and caching actions
- Fresh local Dagger engines with immutable dependency images
- Diagnostic output for cache behavior measurement
- No regression from previous setup
"""

import runpy
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import cast


WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "tests-unit.yml"
PYTEST_DAGGER = (
    Path(__file__).parents[2] / ".github" / "workflows" / "ci" / "pytest_dagger.py"
)
PYTEST_DEPENDENCY_DOCKERFILE = (
    Path(__file__).parents[2]
    / ".github"
    / "workflows"
    / "ci"
    / "pytest-deps.Dockerfile"
)
PYTEST_DEPENDENCY_DOCKERIGNORE = PYTEST_DEPENDENCY_DOCKERFILE.with_name(
    f"{PYTEST_DEPENDENCY_DOCKERFILE.name}.dockerignore",
)
PYTEST_DEPENDENCY_KEY = PYTEST_DEPENDENCY_DOCKERFILE.with_name(
    "pytest_dependency_key.py",
)
PYTEST_DEPENDENCY_PACKER = PYTEST_DEPENDENCY_DOCKERFILE.with_name(
    "pytest_dependency_pack.py",
)
PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"
UV_LOCK = Path(__file__).parents[2] / "uv.lock"
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
    # One invocation owns the native uv cache, controller venv, and uv-managed
    # toolchain. Project dependencies live in the immutable OCI image.
    assert workflow.count("namespacelabs/nscloud-cache-action@") == 1
    assert "gtm-sdk-cache" not in workflow
    assert "placeholder" not in workflow
    assert "cache_key_file" not in workflow
    assert "fingerprint" not in workflow.lower()
    assert "writeback scope" not in workflow.lower()
    assert "UV_PYTHON_INSTALL_DIR" in workflow
    assert "steps.namespace_cache.outputs.cache-hit" in workflow
    # Toolchain + controller venv are siblings under one Namespace mount.
    assert '"$HOME/.dagger-sdk/uv-python"' in workflow
    assert 'dagger_venv="$HOME/.dagger-sdk/controller-venv"' in workflow
    cache_paths = workflow.split("path: |", 1)[1].split("- name:", 1)[0]
    assert "~/.dagger-sdk/controller-venv" in cache_paths
    assert "~/.dagger-sdk/uv-python" in cache_paths
    assert "~/.dagger-sdk/venv" not in cache_paths
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
    assert 'findmnt -R "$HOME/.dagger-sdk/controller-venv"' in workflow
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


def test_unit_workflow_resolves_an_immutable_dependency_image() -> None:
    workflow = WORKFLOW.read_text()

    assert "NSC_CONTAINER_REGISTRY" in workflow
    assert "pytest_dependency_key.py" in workflow
    assert "sha256sum pyproject.toml uv.lock" not in workflow
    assert ".github/workflows/ci/pytest-deps.Dockerfile" in workflow
    assert 'arch="$(uname -m)"' in workflow
    assert "docker manifest inspect --verbose" in workflow
    assert "Compressed dependency image bytes:" in workflow
    assert "Compressed dependency layer:" in workflow
    assert 'reference="${image_tag}@${digest}"' in workflow
    assert '[[ "${digest}" == sha256:* ]]' in workflow
    assert "PYTEST_DEPENDENCY_IMAGE" in workflow
    assert ":latest" not in workflow
    assert "manifest[ _]unknown|no such manifest|name[ _]unknown" in workflow
    assert "manifest unknown|no such manifest|not found" not in workflow


def test_dependency_image_key_ignores_unrelated_project_metadata(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(PYTEST_DEPENDENCY_KEY))
    dependency_image_key = cast(
        Callable[..., str],
        namespace["dependency_image_key"],
    )

    def calculate(pyproject: Path, architecture: str = "arm64") -> str:
        return dependency_image_key(
            pyproject=pyproject,
            uv_lock=UV_LOCK,
            dockerfile=PYTEST_DEPENDENCY_DOCKERFILE,
            dockerignore=PYTEST_DEPENDENCY_DOCKERIGNORE,
            python_version="3.13",
            architecture=architecture,
        )

    original = PYPROJECT.read_text()
    unrelated = tmp_path / "unrelated.toml"
    unrelated.write_text(
        original.replace('gtm = "cli.main:run"', 'gtm = "cli.main:alternate"'),
    )
    dependency_change = tmp_path / "dependency.toml"
    dependency_change.write_text(
        original.replace('"attio>=0.22.8"', '"attio>=0.22.9"'),
    )

    baseline = calculate(PYPROJECT)
    assert len(baseline) == 64
    int(baseline, 16)
    assert calculate(unrelated) == baseline
    assert calculate(dependency_change) != baseline
    assert calculate(PYPROJECT, architecture="amd64") != baseline


def test_dependency_image_key_covers_layout_and_packer(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(PYTEST_DEPENDENCY_KEY))
    dependency_image_key = cast(
        Callable[..., str],
        namespace["dependency_image_key"],
    )

    def calculate(
        layout: str,
        packer: Path = PYTEST_DEPENDENCY_PACKER,
        compression: str = "zstd:3",
    ) -> str:
        return dependency_image_key(
            pyproject=PYPROJECT,
            uv_lock=UV_LOCK,
            dockerfile=PYTEST_DEPENDENCY_DOCKERFILE,
            dockerignore=PYTEST_DEPENDENCY_DOCKERIGNORE,
            packer=packer,
            layout=layout,
            compression=compression,
            python_version="3.13",
            architecture="arm64",
        )

    changed_packer = tmp_path / "pytest_dependency_pack.py"
    changed_packer.write_text(PYTEST_DEPENDENCY_PACKER.read_text() + "\n")

    assert calculate("minimal-packed") != calculate("minimal-expanded")
    assert calculate("minimal-packed") != calculate(
        "minimal-packed",
        changed_packer,
    )
    assert calculate("minimal-compiled") != calculate(
        "minimal-compiled",
        compression="gzip",
    )


def test_unit_workflow_only_publishes_dependency_images_from_trusted_main() -> None:
    workflow = WORKFLOW.read_text()
    normalized = " ".join(workflow.split())

    assert (
        "docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8" in workflow
    )
    assert "# v6.19.2" in workflow
    assert "github.ref == 'refs/heads/main'" in normalized
    assert "github.event_name != 'pull_request'" in normalized
    assert "steps.pytest_dependency_image.outputs.hit != 'true'" in normalized
    assert "platforms: linux/arm64" in workflow
    assert (
        "type=image,name=${{ steps.pytest_dependency_image.outputs.tag }}" in workflow
    )
    assert "push=true" in workflow
    assert "steps.build_pytest_dependency_image.outputs.digest" in workflow
    assert "cache-from" not in workflow
    assert "cache-to" not in workflow
    assert "docker/setup-qemu-action" not in workflow
    assert "docker/setup-buildx-action" not in workflow
    assert "compression=zstd" in workflow
    assert "compression-level=3" in workflow
    assert "force-compression=true" in workflow


def test_unit_workflow_uses_trusted_controller_and_withholds_fork_tokens() -> None:
    workflow = WORKFLOW.read_text()
    resolve_step = workflow.split("- name: Resolve pytest dependency image", 1)[
        1
    ].split("- name: Build and publish pytest dependency image", 1)[0]
    run_step = workflow.split("- name: Run pytest in Dagger", 1)[1].split(
        "- name: Upload Test Results to Trunk.io",
        1,
    )[0]
    assert "- name: Configure Namespace registry access" in workflow
    namespace_setup_step = workflow.split(
        "- name: Configure Namespace registry access",
        1,
    )[1].split("- name: Report Namespace cache state", 1)[0]

    assert (
        "github.event.pull_request.head.repo.full_name == github.repository" in workflow
    )
    assert "id-token: write" not in workflow
    assert "nsc auth exchange-github-token" not in workflow
    assert (
        "namespacelabs/nscloud-setup@df198f982fcecfb8264bea3f1274b56a61b6dfdc"
        in namespace_setup_step
    )
    assert "# v0.0.12" in namespace_setup_step
    assert (
        "steps.selected_pytest_dependency_image.outputs.reference != ''"
        in namespace_setup_step
    )
    assert "nsc auth generate-dev-token" not in workflow
    assert "registry_token_file" not in workflow
    assert 'os.environ["NSC_TOKEN_FILE"]' in run_step
    assert 'token.get("bearer_token", "")' in run_step
    assert 'echo "::add-mask::${registry_token}"' in workflow
    assert "NAMESPACE_REGISTRY_TOKEN" in workflow
    assert "Fork pull request: dependency image disabled" in workflow
    assert 'git show "${BASE_SHA}:.github/workflows/ci/pytest_dagger.py"' in workflow
    assert (
        'git show "${BASE_SHA}:.github/workflows/ci/pytest-deps.Dockerfile"' in workflow
    )
    assert "PYTEST_DEPENDENCY_DOCKERFILE" in workflow
    assert 'dagger run python "${pipeline}"' in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "SAME_REPOSITORY_PR" in workflow
    assert "trusted CI assets are unavailable for a fork pull request" in workflow
    assert "bootstrapping trusted CI assets from this same-repository PR" in workflow
    assert "trusted legacy base SHA without checkpoint packer" in workflow
    assert run_step.index(
        'git show "${BASE_SHA}:.github/workflows/ci/pytest-deps.Dockerfile"',
    ) < run_step.index(
        'git show "${BASE_SHA}:.github/workflows/ci/pytest_dependency_pack.py"',
    )
    assert 'if [ "${PULL_REQUEST_RUN}" = "true" ]; then' in workflow
    assert (
        'git show "${BASE_SHA}:.github/workflows/ci/pytest_dependency_key.py"'
        in resolve_step
    )
    assert 'python "${trusted_key_script}" --help' in resolve_step
    assert 'grep -q -- "--layout"' in resolve_step
    assert resolve_step.index('if [ "${TRUSTED_REGISTRY_PULL}" != "true" ]') < (
        resolve_step.index('image_key="$(python "${key_script}"')
    )
    assert (
        run_step.index("git show")
        < run_step.index('os.environ["NSC_TOKEN_FILE"]')
        < run_step.index('echo "::add-mask::${registry_token}"')
        < run_step.index('dagger run python "${pipeline}"')
    )


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

    assert 'find "${dagger_venv}" -mindepth 1 -delete' in workflow
    assert 'uv venv --python 3.13 "${dagger_venv}"' in workflow
    assert 'uv venv --clear --python 3.13 "${dagger_venv}"' not in workflow
    assert 'rm -rf "${dagger_venv}" "${UV_PYTHON_INSTALL_DIR}"' not in workflow


def test_unit_workflow_uses_dependency_image_and_local_fallbacks() -> None:
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
    assert 'DAGGER_NO_NAG=1 dagger run python "${pipeline}"' in workflow
    assert "DAGGER_CLOUD_COMPUTE_TOKEN" not in workflow
    assert "dagger --cloud" not in workflow
    assert "trunk-io/analytics-uploader@" in workflow
    assert "junit-paths: junit.xml" in workflow
    assert "PYTEST_DEPENDENCY_IMAGE" in dagger
    assert "PYTEST_DEPENDENCY_DOCKERFILE" in dagger
    assert "NAMESPACE_REGISTRY_TOKEN" in dagger
    assert 'if "@sha256:" not in dependency_image' in dagger
    assert "dag.set_secret(" in dagger
    assert '.with_registry_auth(registry_host, "token", registry_secret)' in dagger
    assert ".from_(dependency_image)" in dagger
    assert dagger.index("with_registry_auth") < dagger.index(".from_(dependency_image)")
    assert ".docker_build(" in dagger
    assert "pytest-deps.Dockerfile" in dagger
    assert "Pytest dependency image: unavailable; building cold" in dagger
    assert "dag.host().file(dockerfile_override)" in dagger
    assert '.with_file("pyproject.toml", source.file("pyproject.toml"))' in dagger
    assert '.with_file("uv.lock", source.file("uv.lock"))' in dagger
    assert ".with_file(DEPENDENCY_PACKER_PATH, packer)" in dagger
    assert "publish(" not in dagger
    for forbidden in (
        "HOST_PROJECT_ENV",
        "HOST_UV_PYTHON",
        "HOST_UV_CACHE",
        'dag.cache_volume("uv-cache")',
        "Dagger uv cache: seeding",
        "curl -LsSf https://astral.sh/uv/install.sh",
    ):
        assert forbidden not in dagger
    assert 'dag.host().directory("scripts")' in dagger
    assert 'with_directory("/src", source, owner="runner")' in dagger
    assert 'with_directory("/opt/gtm-sdk/scripts", scripts, owner="runner")' in dagger
    assert '.with_env_variable("PYTHONPATH", "/src")' in dagger
    assert '.with_env_variable("PYTHONDONTWRITEBYTECODE", "1")' in dagger
    assert "--junit-xml=junit.xml" in dagger
    assert "echo $? > /src/pytest_rc" in dagger
    assert "sys.exit(rc)" in dagger
    assert 'await ctr.file("/src/junit.xml").export(JUNIT_HOST_PATH)' in dagger
    assert "actions/cache" not in workflow
    assert "qemu" not in workflow.lower()
    assert "cache-from" not in workflow
    assert "cache-to" not in workflow


def test_local_dagger_commands_provision_the_project_environment() -> None:
    """Local instructions must not rely on an already-created .venv."""
    for path in (PYTEST_DAGGER, PYTEST_INTEGRATION_DAGGER):
        source = path.read_text()
        assert "uv run dagger run python" in source
        assert "dagger run .venv/bin/python" not in source

    ci_validate = (
        Path(__file__).parents[2] / "scripts" / "ci-suite-validate.py"
    ).read_text()
    assert "uv run dagger run python scripts/ci.py" in ci_validate
    assert "dagger run .venv/bin/python scripts/ci.py" not in ci_validate


def test_unit_dagger_keeps_ci_suite_validator_build_interface() -> None:
    dagger = PYTEST_DAGGER.read_text()
    ci_validate = (
        Path(__file__).parents[2] / "scripts" / "ci-suite-validate.py"
    ).read_text()

    assert "pytest_dagger.build_container()" in ci_validate
    assert "def build_container() -> dagger.Container:" in dagger
    assert "return build_containers()[-1]" in dagger


def test_unit_workflow_no_longer_builds_the_project_environment_on_host() -> None:
    workflow = WORKFLOW.read_text()

    assert "Warm project uv cache" not in workflow
    assert "Host project uv cache" not in workflow
    assert 'project_env="$HOME/.dagger-sdk/venv"' not in workflow
    assert 'UV_PROJECT_ENVIRONMENT="${project_env}" uv sync' not in workflow
    assert '--python "${project_env}/bin/python" .' not in workflow
    assert "Install Dagger Python SDK" in workflow
    assert "dagger-io==0.21.7" in workflow
    assert "anyio==4.13.0" in workflow
    assert 'version("dagger-io") == "0.21.7"' in workflow
    assert 'version("anyio") == "4.13.0"' in workflow
    assert "cache: uv" in workflow


def test_unit_workflow_supports_manual_dispatch() -> None:
    # Measurement work (issues #303/#305) needs sequential runs on demand;
    # marker commits pollute history and roborev/PR flows.
    workflow = WORKFLOW.read_text()
    assert "workflow_dispatch:" in workflow


def test_unit_workflow_exposes_checkpoint_benchmark_variants() -> None:
    workflow = WORKFLOW.read_text()

    assert "artifact_variant:" in workflow
    for variant in (
        "full-compiled",
        "full-source",
        "minimal-compiled",
        "minimal-expanded",
        "minimal-packed",
    ):
        assert f"- {variant}" in workflow
    assert "benchmark_nonce:" in workflow
    assert "PYTEST_BENCHMARK_NONCE" in workflow
    assert '--layout "${artifact_variant}"' in workflow


def test_unit_workflow_uses_a_fresh_dagger_engine() -> None:
    workflow = WORKFLOW.read_text()

    assert "- name: Start Dagger engine with persistent state" not in workflow
    assert "- name: Report Dagger engine state" not in workflow
    assert "- name: Start graceful Dagger engine shutdown" not in workflow
    assert "- name: Await graceful Dagger engine shutdown" not in workflow
    assert "_EXPERIMENTAL_DAGGER_RUNNER_HOST" not in workflow
    assert ":/var/lib/dagger" not in workflow
    assert "docker stop" not in workflow
    assert "DAGGER_CLOUD_COMPUTE_TOKEN" not in workflow
    assert "dagger --cloud" not in workflow


def test_unit_dagger_pipeline_validates_the_immutable_environment() -> None:
    workflow = WORKFLOW.read_text()
    dagger = PYTEST_DAGGER.read_text()

    normalized = " ".join(dagger.replace("\\\n", " ").split())
    assert '"--all-extras --dev"' in dagger
    assert 'else "--only-group unit-ci"' in dagger
    assert "--locked --no-install-project --inexact --check" in normalized
    assert (
        "uv pip install --no-deps --reinstall --no-build-isolation --offline " in dagger
    )
    assert '"--python /opt/venv/bin/python ."' in dagger
    check_exec = normalized.index(
        '.with_exec(["bash", "-c", dependency_check_cmd(layout)])',
    )
    install_exec = normalized.index(
        '.with_exec(["bash", "-c", PROJECT_INSTALL_CMD])',
    )
    pytest_exec = normalized.index(
        'tested = installed.with_exec(["bash", "-c", PYTEST_CMD])',
    )
    assert check_exec < install_exec < pytest_exec
    assert "Warm project uv cache" not in workflow
    assert 'dag.cache_volume("venv")' not in dagger
    assert '"/opt/venv/bin/python" -m pytest ' in dagger
    assert '"/src/.venv"' not in dagger


def test_unit_dependency_image_contains_only_locked_dependencies() -> None:
    dockerfile = PYTEST_DEPENDENCY_DOCKERFILE.read_text()
    dockerignore = PYTEST_DEPENDENCY_DOCKERIGNORE.read_text()
    pyproject = tomllib.loads(PYPROJECT.read_text())
    lock = tomllib.loads(UV_LOCK.read_text())

    assert dockerfile.count("@sha256:") >= 2
    assert "python:3.13-slim-bookworm@sha256:" in dockerfile
    assert "ghcr.io/astral-sh/uv:0.11.26@sha256:" in dockerfile
    assert "apt-get install -y --no-install-recommends git time" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "useradd" in dockerfile
    assert "USER runner" in dockerfile
    assert "HEALTHCHECK NONE" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert (
        "COPY --chown=runner:runner --from=dependency-builder /opt/venv /opt/venv"
        in dockerfile
    )
    assert "--all-extras" in dockerfile
    assert "--dev" in dockerfile
    assert "--locked" in dockerfile
    assert "--no-install-project" in dockerfile
    assert "--compile-bytecode" in dockerfile
    assert "COPY . " not in dockerfile
    assert "ADD . " not in dockerfile
    assert "/src" not in dockerfile
    assert dockerignore.splitlines() == [
        "**",
        "!pyproject.toml",
        "!uv.lock",
        "!.github/",
        "!.github/workflows/",
        "!.github/workflows/ci/",
        "!.github/workflows/ci/pytest_dependency_pack.py",
    ]
    assert "setuptools>=83.0.0" in pyproject["dependency-groups"]["dev"]
    locked_setuptools = next(
        package for package in lock["package"] if package["name"] == "setuptools"
    )
    assert locked_setuptools["version"] == "83.0.0"


def test_unit_dependency_image_builds_checkpoint_variants() -> None:
    dockerfile = PYTEST_DEPENDENCY_DOCKERFILE.read_text()
    pyproject = tomllib.loads(PYPROJECT.read_text())
    lock = tomllib.loads(UV_LOCK.read_text())
    root_package = next(
        package
        for package in lock["package"]
        if package["name"] == pyproject["project"]["name"]
    )
    locked_unit_ci = root_package["dev-dependencies"]["unit-ci"]
    locked_unit_ci_metadata = root_package["metadata"]["requires-dev"]["unit-ci"]
    project_unit_ci_names = {
        re.split(r"[\[<>=!~ ]", requirement, maxsplit=1)[0]
        for requirement in pyproject["dependency-groups"]["unit-ci"]
    }

    assert PYTEST_DEPENDENCY_PACKER.is_file()
    assert "unit-ci" in pyproject["dependency-groups"]
    assert {dependency["name"] for dependency in locked_unit_ci} == (
        project_unit_ci_names
    )
    assert {dependency["name"] for dependency in locked_unit_ci_metadata} == (
        project_unit_ci_names
    )
    assert "ARG PYTEST_DEPENDENCY_LAYOUT=minimal-compiled" in dockerfile
    assert "--only-group unit-ci" in dockerfile
    assert "pytest_dependency_pack.py" in dockerfile
    assert "python /build/pytest_dependency_pack.py /opt/venv" in dockerfile
    assert "pytest-deps.zip" in PYTEST_DEPENDENCY_PACKER.read_text()


def test_unit_workflow_reports_four_by_eight_runner_and_phase_diagnostics() -> None:
    workflow = WORKFLOW.read_text()
    dagger = PYTEST_DAGGER.read_text()

    assert "Expected runner shape: 4 vCPU / 8 GiB" in workflow
    assert "aarch64|arm64) ;;" in workflow
    assert "runner architecture must be ARM64" in workflow
    assert "getconf _NPROCESSORS_ONLN" in workflow
    assert "/proc/meminfo" in workflow
    assert "Dependency image digest:" in workflow
    assert "Peak container memory:" in dagger
    assert "Dagger dependency base ready:" in dagger
    assert "Dependency checkpoint bytes:" in dagger
    assert "Dependency checkpoint files:" in dagger
    assert "Dagger dependency check:" in dagger
    assert "Dagger local project install:" in dagger
    assert "Pytest session completed:" in dagger


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

    assert '"/opt/venv/bin/python" -m pytest ' in source
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
