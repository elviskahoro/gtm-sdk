"""Unit contracts for the Dagger Bazel pipeline's MergeGraph request."""

# ruff: noqa: INP001, S101, SLF001 -- workflow tests are standalone and assertion-based.

from __future__ import annotations

import importlib.util
import json
import sys
from email.message import Message
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = REPO_ROOT / ".github" / "workflows" / "ci" / "bazel_dagger.py"
_MODULE_NAME = "_bazel_dagger_under_test"


class _DaggerStub(ModuleType):
    dag: object


def _load_pipeline() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, PIPELINE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.modules[_MODULE_NAME] = module
    try:
        # These tests cover the pipeline's stdlib-only payload helpers. Keep
        # them runnable in the unit dependency image, which need not include
        # the controller-only Dagger SDK and anyio runtime.
        dagger_stub = _DaggerStub("dagger")
        dagger_stub.dag = object()
        with patch.dict(
            sys.modules,
            {"anyio": ModuleType("anyio"), "dagger": dagger_stub},
        ):
            spec.loader.exec_module(module)
    finally:
        sys.modules.pop(_MODULE_NAME, None)
        sys.dont_write_bytecode = old_dont_write_bytecode
    return module


def _payload(module: ModuleType, **overrides: object) -> dict[str, object]:
    options: dict[str, object] = {
        "repository": "sanhedrin/gtm-sdk",
        "pr_number": "42",
        "pr_sha": "deadbeef",
        "target_branch": "main",
        "targets": ["//libs/foo:tests", "//src:all"],
        "changed_paths": ["libs/foo/client.py"],
    }
    options.update(overrides)
    return json.loads(module._impacted_targets_payload(**options))


def test_payload_sends_computed_targets_for_narrow_change() -> None:
    module = _load_pipeline()
    payload = _payload(module)
    assert payload == {
        "repo": {"host": "github.com", "owner": "sanhedrin", "name": "gtm-sdk"},
        "pr": {"number": 42, "sha": "deadbeef"},
        "targetBranch": "main",
        "impactedTargets": ["//libs/foo:tests", "//src:all"],
    }


@pytest.mark.parametrize(
    "path",
    [
        ".trunk/trunk.yaml",
        ".github/workflows/tests-bazel.yml",
        "MODULE.bazel",
        "pyproject.toml",
        "uv.lock",
        "scripts/bazel-requirements-sync.py",
    ],
)
def test_payload_sends_all_for_graph_wide_change(path: str) -> None:
    module = _load_pipeline()
    assert _payload(module, changed_paths=[path])["impactedTargets"] == "ALL"


def test_payload_falls_back_to_all_when_body_is_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_pipeline()
    monkeypatch.setattr(module, "MAX_IMPACTED_TARGETS_BODY_BYTES", 1)
    assert _payload(module)["impactedTargets"] == "ALL"


def test_post_raises_for_trunk_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_pipeline()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise HTTPError(
            module.IMPACTED_TARGETS_URL,
            401,
            "Unauthorized",
            Message(),
            None,
        )

    monkeypatch.setattr(module, "urlopen", fail)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        module._post_impacted_targets("token", b"{}")
