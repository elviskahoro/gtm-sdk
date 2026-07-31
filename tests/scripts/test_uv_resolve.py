"""Unit tests for scripts/lib/uv_resolve.py.

The regression this module exists to fix: a `uv` binary earlier on PATH can
be version-incompatible with this repo's `[tool.uv] required-version` (e.g.
a pyenv shim shadowing a compatible Homebrew install), and `shutil.which`-style
first-match resolution would stop there. Every resolution test below builds a
synthetic PATH with real, executable stub scripts rather than mocking
`subprocess.run` -- matching the convention already used by
`tests/scripts/test_deploy_webhook.py` and `test_conductor_workspace_setup.py`.
"""
# ruff: noqa: S101

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.lib import uv_resolve

REQUIRED_RANGE = ">=0.11.8,<0.12"


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_uv_stub(bin_dir: Path, *, version: str | None, name: str = "uv") -> Path:
    """Write an executable `uv` stub reporting `version` for `--version`.

    `version=None` writes a stub whose `--version` output doesn't match the
    "uv X.Y.Z" pattern at all -- simulating a broken/foreign binary.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    if version is None:
        path.write_text("#!/bin/sh\necho 'not a uv binary' >&2\nexit 3\n")
    else:
        path.write_text(f"#!/bin/sh\necho 'uv {version} (stub)'\n")
    _make_executable(path)
    return path


class TestVersionSatisfies:
    @pytest.mark.parametrize(
        ("version_text", "expected"),
        [
            ("0.11.7", False),  # below lower bound -- the exact incompatible version
            ("0.11.8", True),  # lower bound, inclusive
            ("0.11.29", True),  # comfortably inside the range
            ("0.12.0", False),  # the tuple-padding trap: (0,12) vs (0,12,0)
            ("0.9.0", False),  # well below range
            ("0.13.0", False),  # well above range
        ],
    )
    def test_against_required_range(
        self,
        *,
        version_text: str,
        expected: bool,
    ) -> None:
        clauses = uv_resolve.parse_range(REQUIRED_RANGE)
        version = uv_resolve._parse_version(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            version_text,
        )
        assert uv_resolve.version_satisfies(version, clauses) is expected

    def test_padding_trap_is_not_a_fluke_of_two_part_versions(self) -> None:
        """`<0.12` must reject `0.12` itself, not just `0.12.0`."""
        clauses = uv_resolve.parse_range("<0.12")
        assert uv_resolve.version_satisfies((0, 12), clauses) is False
        assert uv_resolve.version_satisfies((0, 12, 0), clauses) is False
        assert uv_resolve.version_satisfies((0, 11, 99), clauses) is True


class TestParseRange:
    def test_rejects_unrecognized_clause(self) -> None:
        with pytest.raises(ValueError, match="unrecognized version clause"):
            uv_resolve.parse_range("~=0.11.8")

    def test_parses_multiple_comparators(self) -> None:
        clauses = uv_resolve.parse_range(">=0.11.8,<0.12")
        assert clauses == [(">=", (0, 11, 8)), ("<", (0, 12))]


def test_extract_required_version_matches_real_pyproject() -> None:
    """Drift detector: if pyproject.toml's constraint ever changes, this must
    change with it -- the regex must keep understanding the real file's format.
    """
    required = uv_resolve.extract_required_version(
        uv_resolve.PYPROJECT_PATH.read_text(),
    )
    real_toml = tomllib.loads(uv_resolve.PYPROJECT_PATH.read_text())
    assert required == real_toml["tool"]["uv"]["required-version"]


def test_extract_required_version_raises_when_absent() -> None:
    with pytest.raises(ValueError, match="required-version"):
        uv_resolve.extract_required_version("[project]\nname = 'x'\n")


class TestIterCandidatePaths:
    def test_preserves_path_order_and_dedupes_by_realpath(self, tmp_path: Path) -> None:
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        uv_a = _write_uv_stub(dir_a, version="0.11.7")
        _write_uv_stub(dir_b, version="0.11.29")
        # A symlink alias in a third dir pointing at dir_a's binary must not
        # be double-counted.
        dir_c = tmp_path / "c"
        dir_c.mkdir()
        (dir_c / "uv").symlink_to(uv_a)

        path_env = os.pathsep.join([str(dir_a), str(dir_b), str(dir_c)])
        candidates = uv_resolve.iter_candidate_paths(
            cwd=str(tmp_path),
            path_env=path_env,
            fallback_locations=(),
        )

        assert candidates == [str(dir_a / "uv"), str(dir_b / "uv")]

    def test_skips_non_executable_and_missing_entries(self, tmp_path: Path) -> None:
        real_dir = tmp_path / "real"
        _write_uv_stub(real_dir, version="0.11.29")
        missing_dir = tmp_path / "does-not-exist"

        path_env = os.pathsep.join([str(missing_dir), str(real_dir)])
        candidates = uv_resolve.iter_candidate_paths(
            cwd=str(tmp_path),
            path_env=path_env,
            fallback_locations=(),
        )

        assert candidates == [str(real_dir / "uv")]

    def test_appends_fallback_locations_after_path(self, tmp_path: Path) -> None:
        fallback = _write_uv_stub(tmp_path / "fallback", version="0.11.29")
        candidates = uv_resolve.iter_candidate_paths(
            cwd=str(tmp_path),
            path_env="",
            fallback_locations=(str(fallback),),
        )
        assert candidates == [str(fallback)]

    def test_relative_path_entry_resolves_against_cwd_not_ambient_directory(
        self,
        tmp_path: Path,
    ) -> None:
        """A relative PATH entry must resolve against the *given* `cwd`, not
        wherever the test process's own ambient cwd happens to be -- otherwise
        the existence check here and `probe_uv`'s later `--version` call
        (which uses the same `cwd`) could disagree about what the candidate
        even is (the exact gap a roborev pass on this module caught).
        """
        _write_uv_stub(tmp_path / "tools", version="0.11.29")

        candidates = uv_resolve.iter_candidate_paths(
            cwd=str(tmp_path),
            path_env="tools",
            fallback_locations=(),
        )

        assert candidates == [str(tmp_path / "tools" / "uv")]
        # Returned candidates are always absolute -- no ambiguity downstream.
        assert Path(candidates[0]).is_absolute()

    def test_empty_path_segment_means_cwd(self, tmp_path: Path) -> None:
        _write_uv_stub(tmp_path, version="0.11.29")

        candidates = uv_resolve.iter_candidate_paths(
            cwd=str(tmp_path),
            path_env="",
            fallback_locations=(),
        )

        assert candidates == [str(tmp_path / "uv")]


class TestProbeUv:
    def test_parses_real_version_output(self, tmp_path: Path) -> None:
        stub = _write_uv_stub(tmp_path, version="0.11.29")
        candidate = uv_resolve.probe_uv(str(stub), cwd=str(tmp_path))
        assert candidate.version == (0, 11, 29)

    def test_survives_nonzero_exit_and_garbage_output(self, tmp_path: Path) -> None:
        stub = _write_uv_stub(tmp_path, version=None)
        candidate = uv_resolve.probe_uv(str(stub), cwd=str(tmp_path))
        assert candidate.version is None
        assert candidate.raw_output  # captured for the remediation message

    def test_survives_missing_binary(self, tmp_path: Path) -> None:
        candidate = uv_resolve.probe_uv(
            str(tmp_path / "nonexistent"),
            cwd=str(tmp_path),
        )
        assert candidate.version is None

    def test_survives_timeout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _write_uv_stub(tmp_path, version="0.11.29")

        def _raise_timeout(*_args: object, **_kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd=[str(stub)], timeout=5)

        monkeypatch.setattr(uv_resolve.subprocess, "run", _raise_timeout)
        candidate = uv_resolve.probe_uv(str(stub), cwd=str(tmp_path))
        assert candidate.version is None


class TestFindCompatibleUv:
    def test_skips_incompatible_earlier_path_entry(self, tmp_path: Path) -> None:
        """The central regression test: mirrors the real pyenv-then-Homebrew shape."""
        earlier = tmp_path / "earlier-incompatible"
        later = tmp_path / "later-compatible"
        _write_uv_stub(earlier, version="0.11.7")
        _write_uv_stub(later, version="0.11.29")

        path_env = os.pathsep.join([str(earlier), str(later)])
        result = uv_resolve.find_compatible_uv(
            REQUIRED_RANGE,
            cwd=str(tmp_path),
            path_env=path_env,
            fallback_locations=(),
        )

        assert result.path == str(later / "uv")
        assert result.version == (0, 11, 29)

    def test_raises_with_all_candidates_and_range_when_none_compatible(
        self,
        tmp_path: Path,
    ) -> None:
        only = tmp_path / "only"
        _write_uv_stub(only, version="0.11.7")

        with pytest.raises(uv_resolve.NoCompatibleUvError) as exc_info:
            uv_resolve.find_compatible_uv(
                REQUIRED_RANGE,
                cwd=str(tmp_path),
                path_env=str(only),
                fallback_locations=(),
            )

        error = exc_info.value
        assert len(error.tried) == 1
        assert error.tried[0].path == str(only / "uv")
        message = str(error)
        assert str(only / "uv") in message
        assert "0.11.7" in message
        assert REQUIRED_RANGE in message

    def test_raises_when_path_is_entirely_empty(self, tmp_path: Path) -> None:
        with pytest.raises(uv_resolve.NoCompatibleUvError) as exc_info:
            uv_resolve.find_compatible_uv(
                REQUIRED_RANGE,
                cwd=str(tmp_path),
                path_env="",
                fallback_locations=(),
            )
        assert exc_info.value.tried == []


def test_format_remediation_reports_path_version_range_and_fix() -> None:
    tried = [
        uv_resolve.UvCandidate(
            path="/fake/uv",
            version=(0, 11, 7),
            raw_output="uv 0.11.7",
        ),
    ]
    message = uv_resolve.format_remediation(tried, REQUIRED_RANGE)
    assert "/fake/uv" in message
    assert "0.11.7" in message
    assert REQUIRED_RANGE in message
    assert "curl" in message  # a concrete, actionable install command


class TestCli:
    def test_prints_path_only_on_success(self, tmp_path: Path) -> None:
        stub = _write_uv_stub(tmp_path, version="0.11.29")
        env = os.environ.copy()
        env["PATH"] = str(tmp_path)
        result = subprocess.run(  # noqa: S603 — argv list, shell disabled, fixed script path
            [sys.executable, str(uv_resolve.SCRIPT_DIR / "lib" / "uv_resolve.py")],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == str(stub)
        assert result.stderr == ""

    def test_prints_nothing_to_stdout_and_remediation_to_stderr_on_failure(
        self,
        tmp_path: Path,
    ) -> None:
        _write_uv_stub(tmp_path, version="0.11.7")
        env = os.environ.copy()
        env["PATH"] = str(tmp_path)
        env["HOME"] = str(tmp_path / "empty-home")  # keep fallback locations empty too
        result = subprocess.run(  # noqa: S603 — argv list, shell disabled, fixed script path
            [sys.executable, str(uv_resolve.SCRIPT_DIR / "lib" / "uv_resolve.py")],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert result.stdout == ""
        assert "0.11.7" in result.stderr
        assert REQUIRED_RANGE in result.stderr

    def test_uses_cwd_project_when_helper_is_mounted_elsewhere(
        self,
        tmp_path: Path,
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            f'[tool.uv]\nrequired-version = "{REQUIRED_RANGE}"\n',
        )
        helper = tmp_path / "mounted" / "scripts" / "lib" / "uv_resolve.py"
        helper.parent.mkdir(parents=True)
        helper.write_text(
            (uv_resolve.SCRIPT_DIR / "lib" / "uv_resolve.py").read_text(),
        )
        stub = _write_uv_stub(tmp_path / "bin", version="0.11.29")
        env = os.environ.copy()
        env["PATH"] = str(stub.parent)

        result = subprocess.run(  # noqa: S603 — fixed copied helper path
            [sys.executable, str(helper)],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0
        assert result.stdout.strip() == str(stub)
        assert result.stderr == ""
