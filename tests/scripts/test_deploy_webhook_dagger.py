"""Mock-based tests for `_deploy_via_dagger` in scripts/webhooks-redeploy.py.

Complements `tests/scripts/test_deploy_webhook.py`, which exercises only the
host-subprocess fallback (DAGGER_DRY_RUN=1). The live Dagger code path —
secrets wiring, source mount + excludes, and the uv-sync + modal-deploy
container chain — is otherwise unexercised because spinning up a real Dagger
engine in CI would also require live Modal credentials. Instead, we patch
the `dagger` module on the loaded script and assert the SDK call graph.

Each chainable container method (`from_`, `with_directory`, `with_workdir`,
`with_exec`, `with_secret_variable`) returns a *distinct* mock — not the same
parent — so tests can verify that `.sync()` was awaited on the container
produced by the final `with_exec(["uv", "run", "modal", "deploy", ...])`,
not on an earlier link of the chain. Flattening every chain step onto one
mock would let a regression that awaits `sync()` before the modal-deploy
exec slip past the test.

BD: ai-04d. Roborev flagged this gap during the bash→Python rewrite.
"""
# trunk-ignore-all(bandit/B106): hardcoded keyword args are test fixtures, not real credentials.

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "webhooks-redeploy.py"
HANDLER_FILE = REPO_ROOT / "webhooks" / "export_to_attio.py"

_MODULE_NAME = "_webhooks_redeploy_under_test"

# Chainable container methods called by ``_deploy_via_dagger``. Each one must
# return a *new* container so test assertions can distinguish identity along
# the chain (e.g. ``sync()`` must land on the container returned by the final
# ``with_exec``, not on an earlier link).
_CHAINABLE_METHODS: tuple[str, ...] = (
    "from_",
    "with_directory",
    "with_workdir",
    "with_exec",
    "with_secret_variable",
)


@dataclass
class _ChainStep:
    """One link in the container builder chain.

    ``container`` is the MagicMock representing this link. ``produced_by`` is
    the ``(method_name, args, kwargs)`` of the chainable call that minted it
    (``None`` for the root container returned by ``dag.container()``).
    ``parent`` points at the link whose container the chainable call was
    invoked on (``None`` for the root). The chain is reconstructed in order,
    so ``steps[-1]`` is always the final container that the script awaits
    ``sync()`` on.
    """

    container: MagicMock
    produced_by: tuple[str, tuple[object, ...], dict[str, object]] | None = None
    parent: _ChainStep | None = None
    children_seen: list[str] = field(default_factory=list)


@pytest.fixture(scope="module")
def script_module() -> Iterator[ModuleType]:
    """Load scripts/webhooks-redeploy.py as a module without packaging it.

    The script lives under `scripts/`, which is intentionally excluded from
    `[tool.setuptools.packages.find]`, so a normal `import` doesn't resolve.
    Loading via importlib keeps the build config untouched.
    """
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(_MODULE_NAME, None)


def _build_dagger_mock() -> tuple[MagicMock, list[_ChainStep], MagicMock]:
    """Return ``(fake_dagger, steps, src_dir)`` with a per-link container chain.

    Every chainable call on a container spawns a fresh mock and appends a
    new ``_ChainStep`` to ``steps`` — so ``steps[i].container`` is the *i*-th
    link in the build chain. The root container (returned by
    ``dag.container()``) is ``steps[0]``. ``sync`` is configured on *every*
    link so awaiting it never raises, but tests assert that exactly one link
    (the last) was awaited.
    """
    steps: list[_ChainStep] = []

    def _spawn(
        produced_by: tuple[str, tuple[object, ...], dict[str, object]] | None,
        parent: _ChainStep | None,
    ) -> MagicMock:
        idx = len(steps)
        container = MagicMock(name=f"container[{idx}]")
        step = _ChainStep(container=container, produced_by=produced_by, parent=parent)
        steps.append(step)
        for method_name in _CHAINABLE_METHODS:
            # Capture method_name/step in default args so each closure binds
            # its own values (don't share via the enclosing loop vars).
            def _chained(
                *args: object,
                _method: str = method_name,
                _parent: _ChainStep = step,
                **kwargs: object,
            ) -> MagicMock:
                _parent.children_seen.append(_method)
                return _spawn((_method, args, kwargs), _parent)

            getattr(container, method_name).side_effect = _chained
        container.sync = AsyncMock(name=f"sync[{idx}]")
        return container

    src_dir = MagicMock(name="src_dir")

    def _mint_secret(name: str, value: str) -> MagicMock:
        return MagicMock(_secret=(name, value), name=f"secret[{name}]")

    dag = MagicMock(name="dag")
    dag.set_secret.side_effect = _mint_secret
    dag.container.side_effect = lambda: _spawn(None, None)
    dag.host.return_value.directory.return_value = src_dir

    connection_cm = MagicMock(name="connection_cm")
    connection_cm.__aenter__ = AsyncMock(return_value=None)
    connection_cm.__aexit__ = AsyncMock(return_value=None)

    fake_dagger = MagicMock(name="dagger_module")
    fake_dagger.connection.return_value = connection_cm
    fake_dagger.Config = MagicMock(name="Config")
    fake_dagger.dag = dag

    return fake_dagger, steps, src_dir


def _step_methods(steps: list[_ChainStep]) -> list[str | None]:
    """Return the chainable method names that produced each step.

    ``[None, "from_", "with_directory", ...]`` — first entry is ``None`` for
    the root container minted by ``dag.container()``.
    """
    return [step.produced_by[0] if step.produced_by else None for step in steps]


def _secret_links(
    steps: list[_ChainStep],
) -> list[tuple[_ChainStep, tuple[object, ...]]]:
    """Filter steps to ``with_secret_variable`` links and unpack their args.

    Returns ``[(step, args), ...]`` so callers can read positional args (the
    env-var name and the dagger secret object) without re-narrowing
    ``step.produced_by`` from ``Optional`` at each call site.
    """
    out: list[tuple[_ChainStep, tuple[object, ...]]] = []
    for step in steps:
        produced = step.produced_by
        if produced is None or produced[0] != "with_secret_variable":
            continue
        out.append((step, produced[1]))
    return out


@pytest.mark.asyncio
async def test_deploy_via_dagger_with_host(script_module: ModuleType) -> None:
    """All six secrets wire through when INFISICAL_HOST is provided."""
    fake_dagger, steps, _src_dir = _build_dagger_mock()

    with patch.object(script_module, "dagger", fake_dagger):
        await script_module._deploy_via_dagger(
            handler_file=HANDLER_FILE,
            modal_token_id="mtok-id",
            modal_token_secret="mtok-secret",
            infisical_token="inf-token",
            infisical_project_id="inf-proj",
            infisical_env="dev",
            infisical_host="https://app.infisical.com",
        )

    expected_secret_calls = [
        ("modal-token-id", "mtok-id"),
        ("modal-token-secret", "mtok-secret"),
        ("infisical-token", "inf-token"),
        ("infisical-project-id", "inf-proj"),
        ("infisical-env", "dev"),
        ("infisical-host", "https://app.infisical.com"),
    ]
    actual_secret_calls = [
        (call.args[0], call.args[1])
        for call in fake_dagger.dag.set_secret.call_args_list
    ]
    assert actual_secret_calls == expected_secret_calls

    # Each ``with_secret_variable`` lives on its own chain link; collect them
    # in order and assert the (env-var-name, dagger-secret) pairs are wired
    # to the right credential — guards against a regression that wires
    # MODAL_TOKEN_ID to the infisical-token secret, etc.
    actual_env_calls = [
        (args[0], cast(MagicMock, args[1])._secret)
        for _step, args in _secret_links(steps)
    ]
    expected_env_calls = [
        (_env_for(name), (name, value)) for name, value in expected_secret_calls
    ]
    assert actual_env_calls == expected_env_calls


@pytest.mark.asyncio
async def test_deploy_via_dagger_without_host(script_module: ModuleType) -> None:
    """INFISICAL_HOST is omitted entirely when not set on the host.

    Guards `libs/infisical` against being handed a fabricated empty string
    that would confuse self-host vs. SaaS detection.
    """
    fake_dagger, steps, _ = _build_dagger_mock()

    with patch.object(script_module, "dagger", fake_dagger):
        await script_module._deploy_via_dagger(
            handler_file=HANDLER_FILE,
            modal_token_id="mtok-id",
            modal_token_secret="mtok-secret",
            infisical_token="inf-token",
            infisical_project_id="inf-proj",
            infisical_env="dev",
            infisical_host=None,
        )

    set_secret_names = [
        call.args[0] for call in fake_dagger.dag.set_secret.call_args_list
    ]
    assert "infisical-host" not in set_secret_names
    assert len(set_secret_names) == 5

    env_names = [args[0] for _step, args in _secret_links(steps)]
    assert "INFISICAL_HOST" not in env_names
    assert len(env_names) == 5


@pytest.mark.asyncio
async def test_deploy_via_dagger_container_chain(script_module: ModuleType) -> None:
    """The container chain runs in the documented order on distinct links.

    Asserts the static container shape: base image, source mount path,
    workdir, `uv sync --frozen`, five secret wirings, and the final
    `uv run modal deploy <rel>` invocation. `rel` must be the POSIX
    repo-relative path so the container can resolve it under /repo
    regardless of host OS path semantics. ``sync()`` must be awaited on
    the *final* link — not the root, not an intermediate one.
    """
    fake_dagger, steps, src_dir = _build_dagger_mock()

    with patch.object(script_module, "dagger", fake_dagger):
        await script_module._deploy_via_dagger(
            handler_file=HANDLER_FILE,
            modal_token_id="mtok-id",
            modal_token_secret="mtok-secret",
            infisical_token="inf-token",
            infisical_project_id="inf-proj",
            infisical_env="dev",
            infisical_host=None,
        )

    # The exact chainable method that produced each link, in order. Pinning
    # the full sequence catches any regression that inserts, drops, or
    # reorders steps in the builder chain.
    assert _step_methods(steps) == [
        None,  # dag.container()
        "from_",
        "with_exec",  # apt-get update && apt-get install ... git (ai-8h3)
        "with_directory",
        "with_workdir",
        "with_exec",  # uv sync --frozen
        "with_secret_variable",  # MODAL_TOKEN_ID
        "with_secret_variable",  # MODAL_TOKEN_SECRET
        "with_secret_variable",  # INFISICAL_TOKEN
        "with_secret_variable",  # INFISICAL_PROJECT_ID
        "with_secret_variable",  # INFISICAL_ENV
        "with_exec",  # uv run modal deploy <rel>
    ]

    fake_dagger.dag.container.assert_called_once_with()

    # Args fed into each link (root has no producer, so skip it).
    args_by_method: dict[str, list[tuple[object, ...]]] = {
        m: [] for m in _CHAINABLE_METHODS
    }
    for step in steps:
        if step.produced_by is None:
            continue
        method, args, _kwargs = step.produced_by
        args_by_method[method].append(args)

    assert args_by_method["from_"] == [(script_module.DAGGER_BASE_IMAGE,)]
    assert args_by_method["with_directory"] == [("/repo", src_dir)]
    assert args_by_method["with_workdir"] == [("/repo",)]
    # git is installed (single combined update+install exec) before the source
    # mount and the sync; without it `uv sync --frozen` cannot clone the
    # public `gtm-linear` git dependency (ai-8h3).
    git_install = [
        "sh",
        "-c",
        "apt-get update && apt-get install -y --no-install-recommends git",
    ]
    final_modal_deploy = ["uv", "run", "modal", "deploy", "webhooks/export_to_attio.py"]
    assert args_by_method["with_exec"] == [
        (git_install,),
        (["uv", "sync", "--frozen"],),
        (final_modal_deploy,),
    ]

    # Regression guard (ai-8h3): git must be installed BEFORE `uv sync
    # --frozen`, otherwise uv cannot resolve the `gtm-linear` git dependency
    # and the deploy aborts with "Git executable not found" before modal
    # deploy runs. Pin the relative ordering explicitly so a future reorder
    # fails loudly instead of silently regressing the fix.
    exec_cmds = [args[0] for args in args_by_method["with_exec"]]
    assert exec_cmds.index(git_install) < exec_cmds.index(["uv", "sync", "--frozen"]), (
        "git install must precede `uv sync --frozen` (ai-8h3)"
    )

    # Source mount excludes both worktree-shape `.git` variants plus build
    # artifacts that would inflate the upload and (for `.venv/`) break
    # `uv sync --frozen` reproducibility inside the container.
    # ``assert_called_once_with`` (not ``call_args``) so a regression that
    # makes a second ``directory()`` call with the right args after a wrong
    # first call still trips the assertion.
    fake_dagger.dag.host.return_value.directory.assert_called_once_with(
        str(script_module.REPO_ROOT),
        exclude=[
            ".venv/",
            "tmp/",
            "**/__pycache__/",
            "*.pyc",
            ".git",
            ".git/",
        ],
    )

    # Parent/child provenance: every link's parent must be the *immediately
    # preceding* link. Catches a regression that drops the ``container =``
    # reassignment inside the secret loop (in which case every secret hangs
    # off the same parent and the final ``with_exec`` ends up rooted on an
    # earlier link instead of the last secret-injected one).
    assert steps[0].parent is None
    for i in range(1, len(steps)):
        parent = steps[i].parent
        expected_parent = steps[i - 1]
        if parent is not expected_parent:
            actual_idx = steps.index(parent) if parent is not None else "?"
            msg = (
                f"step[{i}] ({_step_methods(steps)[i]}) hangs off "
                f"step[{actual_idx}], expected step[{i - 1}]"
            )
            raise AssertionError(msg)

    # sync() must be awaited on the final link (the modal-deploy with_exec
    # result) and on no other. Catches a regression that awaits sync() on
    # an earlier container — which would skip the modal deploy entirely or
    # deploy without secrets injected.
    final_link = steps[-1]
    final_produced = final_link.produced_by
    assert final_produced is not None
    assert final_produced[0] == "with_exec"
    assert final_produced[1] == (final_modal_deploy,)
    final_link.container.sync.assert_awaited_once()
    awaited_elsewhere = [
        i for i, step in enumerate(steps[:-1]) if step.container.sync.await_count > 0
    ]
    assert awaited_elsewhere == []

    # The dagger.connection() context manager is entered with a Config
    # routing logs to stderr (so deploy progress is visible to the operator
    # but never mixed into stdout).
    fake_dagger.Config.assert_called_once_with(log_output=sys.stderr)
    fake_dagger.connection.assert_called_once_with(fake_dagger.Config.return_value)


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        pytest.param(None, None, id="unset"),
        pytest.param("", None, id="empty-string"),
        pytest.param(
            "https://app.infisical.com",
            "https://app.infisical.com",
            id="set",
        ),
        pytest.param("https://self.hosted/", "https://self.hosted/", id="self-host"),
    ],
)
def test_resolve_infisical_host_coerces_unset_and_empty(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
    expected: str | None,
) -> None:
    """Both unset *and* empty-string INFISICAL_HOST must collapse to None.

    A regression that forwards ``""`` as-is would bake an empty
    INFISICAL_HOST into the runtime bootstrap secret, which confuses
    ``libs/infisical`` self-host vs. SaaS detection on the first webhook
    event. This exercises the env coercion directly so the bug can't hide
    behind a default-arg in ``_deploy_via_dagger``'s mocked test.
    """
    if env_value is None:
        monkeypatch.delenv("INFISICAL_HOST", raising=False)
    else:
        monkeypatch.setenv("INFISICAL_HOST", env_value)
    assert script_module._resolve_infisical_host() == expected


def _env_for(secret_name: str) -> str:
    """Map a dagger secret name back to its expected container env var name."""
    return {
        "modal-token-id": "MODAL_TOKEN_ID",
        "modal-token-secret": "MODAL_TOKEN_SECRET",
        "infisical-token": "INFISICAL_TOKEN",
        "infisical-project-id": "INFISICAL_PROJECT_ID",
        "infisical-env": "INFISICAL_ENV",
        "infisical-host": "INFISICAL_HOST",
    }[secret_name]
