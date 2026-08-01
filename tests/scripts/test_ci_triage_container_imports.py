# ruff: noqa: S101 -- asserts are the point of a test file.

"""The triage scripts must import in the minimal tree their containers mount.

``triage_dagger.py`` and ``triage_diagnose_dagger.py`` each mount a handful of
paths -- the one script, and (for the filing pipeline) ``libs/linear`` -- into a
bare ``python:3.13-slim``. No ``scripts/lib/``, no ``pyproject.toml``, no repo.

Commit a43d3e6 added an unconditional ``from scripts.lib.uv_bootstrap import
bootstrap_uv`` to both scripts. Nothing in the suite mounted a partial tree, so
nothing noticed that both containerized paths now died with
``ModuleNotFoundError: No module named 'scripts.lib'`` before ``main()`` ran --
every ticket silently fell through to the host-fallback step, and the Dagger
path that the pipeline docstrings describe as primary had stopped executing.

This reconstructs that tree on disk and runs the script in it. The scripts are
allowed to *use* the bootstrap when the repo is present; they are not allowed to
*require* it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# (script, extra top-level packages the container mounts alongside it). Keep in
# lockstep with the `dag.host().directory(...)` calls in the matching pipeline.
CONTAINER_TREES = [
    pytest.param("ci-triage-linear-issue.py", ["libs"], id="linear-filing"),
    pytest.param("ci-triage-diagnose.py", [], id="oz-diagnose"),
]


def _build_tree(root: Path, script_name: str, packages: list[str]) -> Path:
    """Materialise exactly what the Dagger pipeline puts in the container."""
    (root / "scripts").mkdir()
    shutil.copy(REPO_ROOT / "scripts" / script_name, root / "scripts" / script_name)
    for package in packages:
        # `libs/__init__.py` + `libs/linear/**`, matching the pipeline's include
        # filter -- not the whole package tree.
        shutil.copy(REPO_ROOT / package / "__init__.py", _mkdir(root / package))
        shutil.copytree(REPO_ROOT / package / "linear", root / package / "linear")
    return root / "scripts" / script_name


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# `uv sync` installs this repo as an editable, and setuptools' editable shim is a
# sys.meta_path finder that resolves `scripts.*` (and `libs.*`) from the real
# checkout no matter what the cwd is. Left in place it silently satisfies the
# very imports this test exists to prove are unnecessary -- the pre-fix scripts
# pass. Dropping the finder is what turns tmp_path into an honest stand-in for
# `python:3.13-slim`; third-party site-packages (gtm_linear, oz_agent_sdk) stay,
# because the container pip-installs those.
_DROP_EDITABLE_FINDER = """
import runpy, sys
sys.meta_path = [
    f for f in sys.meta_path
    if not type(f).__module__.startswith("__editable__")
]
# `python -c cmd a b` leaves argv[0] == "-c"; restore the shape argparse expects.
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@pytest.mark.parametrize(("script_name", "packages"), CONTAINER_TREES)
def test_script_runs_without_the_repo_around_it(
    tmp_path: Path,
    script_name: str,
    packages: list[str],
) -> None:
    script = _build_tree(tmp_path, script_name, packages)
    assert not (tmp_path / "scripts" / "lib").exists(), "the tree must stay minimal"
    assert not (tmp_path / "pyproject.toml").exists()

    # `--help` exercises every module-level import and argparse setup, then
    # exits before any credential or network use.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _DROP_EDITABLE_FINDER, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"{script_name} cannot start in its own container's tree:\n{result.stderr}"
    )
    assert "usage:" in result.stdout


@pytest.mark.parametrize(("script_name", "packages"), CONTAINER_TREES)
def test_bootstrap_is_still_used_when_the_repo_is_present(
    script_name: str,
    packages: list[str],  # noqa: ARG001
) -> None:
    """The guard must degrade the bootstrap, not delete it.

    Direct host execution still needs the re-exec through a compatible ``uv`` --
    ``[tool.uv] required-version`` makes an incompatible one refuse before Python
    starts, which a ``uv run`` shebang cannot recover from.
    """
    source = (REPO_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
    assert "from scripts.lib.uv_bootstrap import bootstrap_uv" in source
    assert '_bootstrap_uv(script_path=__file__, mode="script")' in source
    assert "except (ImportError, OSError)" in source, (
        "the import must be guarded, or the container path breaks again"
    )
