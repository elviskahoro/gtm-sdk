"""Deterministic pytest wrapper for Bazel Python test targets."""

load("@rules_python//python:defs.bzl", _py_test = "py_test")

_PYTEST_MAIN = "//bazel:pytest_main.py"
_PYPROJECT_TOML = "//:pyproject.toml"
_PYTEST_DEPS = [
    "@pypi//pytest",
    "@pypi//pytest_asyncio",
]

def pytest_test(
        name,
        srcs = [],
        # buildifier: disable=unused-variable
        main = None,
        deps = [],
        data = [],
        args = [],
        tags = [],
        **kwargs):
    """Run pytest through the shared launcher while preserving generated attrs."""
    test_paths = [_repo_relative_path(src) for src in srcs if src.endswith(".py")]

    _py_test(
        name = name,
        srcs = _dedupe(srcs + [_PYTEST_MAIN]),
        main = _PYTEST_MAIN,
        args = test_paths + args,
        data = _dedupe(data + [_PYPROJECT_TOML]),
        deps = _dedupe(deps + _PYTEST_DEPS),
        tags = tags,
        legacy_create_init = False,
        **kwargs
    )

def _repo_relative_path(src):
    package = native.package_name()
    if package:
        return package + "/" + src
    return src

def _dedupe(items):
    result = []
    seen = {}
    for item in items:
        if item not in seen:
            seen[item] = None
            result.append(item)
    return result
