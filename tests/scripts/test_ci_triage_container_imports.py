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


# Making tmp_path an honest stand-in for `python:3.13-slim` means `scripts.*` and
# `libs.*` must resolve ONLY from the reconstructed tree. Two different mechanisms
# otherwise leak the real checkout in, and each defeats the test silently by
# satisfying the imports it exists to prove are unnecessary:
#
#   * locally, `uv sync` installs the repo editable, and setuptools' editable
#     shim is a sys.meta_path finder that answers regardless of cwd;
#   * in unit CI, pytest_dagger.py installs the project NON-editable
#     (`uv pip install ... .`), so `scripts/` and `libs/` are real packages in
#     /opt/venv site-packages. A regular package anywhere on sys.path beats a
#     namespace portion, so path order cannot save us.
#
# Dropping sys.path entries is not an option either: `gtm_linear` lives in the
# same site-packages, and the container really does pip-install that. So the
# block has to be per-module -- reject a `scripts`/`libs` spec whose origin sits
# outside the tree, and let everything else through untouched.
_BLOCK_OUT_OF_TREE_REPO_PACKAGES = """
import pathlib, runpy, sys
from importlib.abc import MetaPathFinder

# `python -c cmd a b` leaves argv[0] == "-c"; restore the shape argparse expects.
sys.argv = sys.argv[1:]
TREE = pathlib.Path(sys.argv[0]).resolve().parents[1]


class OnlyFromTree(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] not in ("scripts", "libs"):
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is None:
                continue
            locations = [
                p
                for p in [spec.origin, *(spec.submodule_search_locations or [])]
                if p and p != "namespace"
            ]
            if locations and not any(
                pathlib.Path(p).resolve().is_relative_to(TREE) for p in locations
            ):
                msg = f"{fullname} resolved outside the container tree: {locations}"
                raise ModuleNotFoundError(msg, name=fullname)
            return spec
        return None


sys.meta_path.insert(0, OnlyFromTree())
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
        [
            sys.executable,
            "-c",
            _BLOCK_OUT_OF_TREE_REPO_PACKAGES,
            str(script),
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"{script_name} cannot start in its own container's tree:\n{result.stderr}"
    )
    assert "usage:" in result.stdout.casefold()


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
