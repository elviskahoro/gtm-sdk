"""Deterministic ``pytest_test`` macro for Gazelle-generated ``py_test`` targets.

PR2 Task 9 (plan ai-m4p9). Gazelle maps every ``py_test`` it generates to
``pytest_test`` via the root ``# gazelle:map_kind py_test pytest_test
//bazel:pytest_test.bzl`` directive (added in a later plan task). This macro
pins the parts of a test target that Gazelle cannot infer -- the shared pytest
entrypoint, the pytest/pytest-asyncio deps, the authoritative ``pyproject.toml``
runfile, and the importlib ``legacy_create_init`` mode -- while forwarding
every generated attribute (``srcs``/``deps``/``data``/``args``/``tags``/
``kwargs``) unchanged. Hand-written targets call the macro directly with the
same shape, so generated and manual targets are indistinguishable.

Gazelle also emits a ``main`` pointing at one of the test's own ``srcs``; that
is ignored -- every ``pytest_test`` runs through ``//bazel:pytest_main.py``.
"""

load("@rules_python//python:defs.bzl", "py_test")

# The shared entrypoint is exported by bazel/BUILD.bazel; a root-relative label
# resolves identically from every package that calls this macro.
_PYTEST_MAIN = "//bazel:pytest_main.py"

# pyproject.toml (exported by the root BUILD.bazel) is the launcher's ``-c``
# config runfile; it must land in every test target's runfiles.
_PYPROJECT_TOML = "//:pyproject.toml"

# pytest + pytest-asyncio are needed by every test. The pip hub is ``@pip``
# (MODULE.bazel ``hub_name = "pip"``); Gazelle only knows import edges it can
# see, so attach them deterministically here instead of per-target.
_PYTEST_DEPS = [
    "@pip//pytest",
    "@pip//pytest_asyncio",
]

def pytest_test(
        name,
        srcs = [],
        # buildifier: disable=unused-variable
        # Gazelle emits main=<test src>; intentionally unused -- every target
        # runs through //bazel:pytest_main.py (pinned below).
        main = None,
        deps = [],
        data = [],
        args = [],
        tags = [],
        **kwargs):
    """A ``py_test`` that runs pytest through the shared Bazel entrypoint.

    Args:
        name: target name (Gazelle-generated, preserved).
        srcs: test source files (Gazelle-generated, preserved). Also form the
            pytest collection paths passed to the entrypoint.
        main: ignored. Gazelle points this at a test src; every target instead
            runs through ``//bazel:pytest_main.py``.
        deps: additional deps (Gazelle-generated import edges, preserved).
        data: additional runfiles (Gazelle-generated, preserved).
        args: additional pytest args forwarded to the entrypoint (preserved).
        tags: test tags (preserved).
        **kwargs: forwarded verbatim to ``py_test`` (visibility, env, ...).
    """

    # Test files live in the calling package; pass them to pytest as
    # workspace-root-relative paths so importlib mode + ``chdir(root)`` in the
    # entrypoint resolve them exactly as ``uv run pytest <paths>`` would.
    package = native.package_name()
    test_paths = ["%s/%s" % (package, src) for src in srcs]

    py_test(
        name = name,
        srcs = srcs,
        main = _PYTEST_MAIN,
        deps = _dedup(deps + _PYTEST_DEPS),
        data = _dedup(data + [_PYPROJECT_TOML]),
        args = args + test_paths,
        tags = tags,
        legacy_create_init = False,
        **kwargs
    )

def _dedup(items):
    """Order-preserving dedup for label lists (deps/data may overlap)."""
    seen = {}
    for item in items:
        seen[item] = None
    return list(seen.keys())
