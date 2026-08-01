"""Mock-based tests for both deploy executors in scripts/webhooks-handlers-redeploy.py.

Complements the host-side focused tests and the outer-controller real-container
smoke stage. These tests pin secrets wiring, source mount exclusions, and the
uv-sync + modal-deploy SDK call graph without deploying live Modal resources.

They also pin *parity*: `_deploy_via_dagger` and `_deploy_via_flox` must run
the same `deploy_steps()` with the same credential surface, differing only in
the isolation layer. The two executors drifted once already — Flox skipped
`uv sync --frozen` entirely and wrapped the deploy in `infisical run`, whose
whole-environment injection is baked into the app's Modal Secret by
`src/secrets_bootstrap.py` — so the parity assertions below are the mechanism
that keeps "one recipe, two executors" true rather than aspirational.

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
# ruff: noqa: S101, S106, SLF001 -- asserts are the point of a test file, the
# fake tokens are fixtures, and pinning the recipe means reaching for the
# script's private executors and key tuples on purpose.

from __future__ import annotations

import importlib.util
import subprocess
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
SCRIPT_PATH = REPO_ROOT / "scripts" / "webhooks-handlers-redeploy.py"
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
    """Load scripts/webhooks-handlers-redeploy.py as a module without packaging it.

    `scripts*` IS included in `[tool.setuptools.packages.find]`; what blocks a
    plain `import` is the dash in the filename, which is not a legal Python
    identifier. importlib is the only way in short of renaming the script.
    """
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
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


def test_content_addressed_secret_name_is_stable_and_value_sensitive(
    script_module: ModuleType,
) -> None:
    """Same (base, value) is deterministic; a changed value changes the name.

    Guards the actual cache-busting property the helper exists for — a
    regression that returns the base name unhashed, or hashes something
    other than ``value``, would leave Dagger's ``with_exec`` cache keyed on
    a rotated credential's *name* again, silently replaying a stale
    ``modal deploy`` result (the bug this helper fixes).
    """
    mk_name = script_module._content_addressed_secret_name  # trunk-ignore(ruff/SLF001)

    first = mk_name("infisical-token", "token-a")
    second = mk_name("infisical-token", "token-a")
    third = mk_name("infisical-token", "token-b")

    assert first == second  # trunk-ignore(ruff/S101)
    assert first != third  # trunk-ignore(ruff/S101)
    assert first.startswith("infisical-token-")  # trunk-ignore(ruff/S101)
    assert first == "infisical-token-a70bf50e531c"  # trunk-ignore(ruff/S101)


def _deploy_env(script_module: ModuleType, *, host: str | None) -> dict[str, str]:
    """Build the canonical credential dict both executors are handed."""
    return script_module.deploy_env(
        modal_token_id="mtok-id",
        modal_token_secret="mtok-secret",
        infisical_token="inf-token",
        infisical_project_id="inf-proj",
        infisical_env="dev",
        infisical_host=host,
    )


@pytest.mark.asyncio
async def test_deploy_via_dagger_with_host(script_module: ModuleType) -> None:
    """All six secrets wire through when INFISICAL_HOST is provided."""
    fake_dagger, steps, _src_dir = _build_dagger_mock()

    with patch.object(script_module, "dagger", fake_dagger):
        await script_module._deploy_via_dagger(
            HANDLER_FILE,
            deploy_env=_deploy_env(script_module, host="https://app.infisical.com"),
        )

    mk_name = script_module._content_addressed_secret_name  # trunk-ignore(ruff/SLF001)
    base_and_values = [
        ("modal-token-id", "mtok-id"),
        ("modal-token-secret", "mtok-secret"),
        ("infisical-token", "inf-token"),
        ("infisical-project-id", "inf-proj"),
        ("infisical-env", "dev"),
        ("infisical-host", "https://app.infisical.com"),
    ]
    expected_secret_calls = [
        (mk_name(base, value), value) for base, value in base_and_values
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
        (args[0], cast("MagicMock", args[1])._secret)
        for _step, args in _secret_links(steps)
    ]
    expected_env_calls = [
        (_env_for(base), (mk_name(base, value), value))
        for base, value in base_and_values
    ]
    assert actual_env_calls == expected_env_calls


@pytest.mark.asyncio
async def test_deploy_via_dagger_without_host(script_module: ModuleType) -> None:
    """INFISICAL_HOST is omitted entirely when not set on the host.

    Guards `libs/infisical` against being handed a fabricated empty string
    that would confuse self-host vs. SaaS detection.
    """
    fake_dagger, steps, _ = _build_dagger_mock()
    deploy_env = _deploy_env(script_module, host=None)

    with patch.object(script_module, "dagger", fake_dagger):
        await script_module._deploy_via_dagger(HANDLER_FILE, deploy_env=deploy_env)

    set_secret_names = [
        call.args[0] for call in fake_dagger.dag.set_secret.call_args_list
    ]
    assert "infisical-host" not in set_secret_names
    # Counted against the recipe, not a literal 5: the container must carry
    # exactly the credential surface it was handed, no more and no fewer.
    assert len(set_secret_names) == len(deploy_env)

    env_names = [args[0] for _step, args in _secret_links(steps)]
    assert "INFISICAL_HOST" not in env_names
    assert env_names == list(deploy_env)


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
            HANDLER_FILE,
            deploy_env=_deploy_env(script_module, host=None),
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
    assert script_module.DAGGER_BASE_IMAGE == (  # trunk-ignore(ruff/S101)
        "ghcr.io/astral-sh/uv:0.11.29-python3.13-trixie-slim"
    )

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


# ---------------------------------------------------------------------------
# Executor parity
# ---------------------------------------------------------------------------

_REL = "webhooks/export_to_attio.py"


def _dagger_steps_with_credentials(
    steps: list[_ChainStep],
) -> list[tuple[list[str], list[str]]]:
    """Return ``[(argv, sorted credential env names visible), ...]`` per exec.

    Walks each ``with_exec`` link back up its parent chain counting the
    ``with_secret_variable`` links above it, so the result says what the
    container could actually see *at that step* — the property that makes
    ``uv sync --frozen`` credential-free rather than merely late.
    """
    out: list[tuple[list[str], list[str]]] = []
    for step in steps:
        produced = step.produced_by
        if produced is None or produced[0] != "with_exec":
            continue
        argv = cast("list[str]", produced[1][0])
        visible: list[str] = []
        ancestor = step.parent
        while ancestor is not None:
            ancestor_produced = ancestor.produced_by
            if (
                ancestor_produced is not None
                and ancestor_produced[0] == "with_secret_variable"
            ):
                visible.append(cast("str", ancestor_produced[1][0]))
            ancestor = ancestor.parent
        out.append((argv, sorted(visible)))
    return out


def _run_flox(
    script_module: ModuleType,
    deploy_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[list[str], dict[str, object]]]:
    """Invoke ``_deploy_via_flox`` against a stubbed ``subprocess`` module.

    Patches ``script_module.subprocess`` wholesale, never
    ``script_module.subprocess.run`` — the script does ``import subprocess``,
    so that attribute is the real stdlib module and patching it would leak
    into every other test in the session.

    Returns ``(argv, kwargs)`` per call. Unpacking the sole positional
    argument here rather than at each call site keeps the argv typed as a
    list, so the parity assertions can slice off the activation prefix.
    """
    fake_subprocess = MagicMock(name="subprocess")
    monkeypatch.setattr(script_module, "subprocess", fake_subprocess)
    script_module._deploy_via_flox(HANDLER_FILE, deploy_env=deploy_env)
    return [
        (cast("list[str]", call.args[0]), call.kwargs)
        for call in fake_subprocess.run.call_args_list
    ]


def test_deploy_steps_matches_the_literal_commands(script_module: ModuleType) -> None:
    """The recipe holds the exact argvs, spelled out.

    Sourcing the parity expectations only from ``deploy_steps()`` would make
    them ``f(x) == f(x)`` — a typo in the recipe would deploy the typo and
    every assertion would still pass. This is the one place the commands are
    written independently.
    """
    steps = script_module.deploy_steps(_REL)

    assert [step.argv for step in steps] == [
        ["uv", "sync", "--frozen"],
        ["uv", "run", "modal", "deploy", _REL],
    ]
    # Only the deploy step may see credentials: Dagger attaches its secrets
    # after the sync, and a flat recipe would silently let Flox hand them to
    # both.
    assert [step.with_credentials for step in steps] == [False, True]
    assert script_module.GIT_INSTALL_EXEC == (
        "sh",
        "-c",
        "apt-get update && apt-get install -y --no-install-recommends git",
    )
    assert script_module.flox_activate_prefix(script_module.REPO_ROOT) == [
        "flox",
        "activate",
        "--dir",
        str(script_module.REPO_ROOT),
        "--mode",
        "run",
        "--",
    ]


@pytest.mark.asyncio
async def test_executors_run_the_same_recipe(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both executors run ``deploy_steps()`` with the same credential surface.

    The comparison is per step, on ``(argv, sorted(credential env names))``.
    Comparing the union across the whole run would pass for an executor that
    handed credentials to ``uv sync`` too.
    """
    deploy_env = _deploy_env(script_module, host="https://app.infisical.com")
    steps = script_module.deploy_steps(_REL)

    fake_dagger, chain, _src = _build_dagger_mock()
    with patch.object(script_module, "dagger", fake_dagger):
        await script_module._deploy_via_dagger(HANDLER_FILE, deploy_env=deploy_env)
    dagger_execs = _dagger_steps_with_credentials(chain)

    flox_calls = _run_flox(script_module, deploy_env, monkeypatch)

    # Dagger prepends its own git install; that is isolation-layer setup (the
    # Flox environment already pins git via the Nix store), not a recipe step.
    assert [argv for argv, _ in dagger_execs] == [
        list(script_module.GIT_INSTALL_EXEC),
        *[step.argv for step in steps],
    ]
    assert [argv for argv, _ in flox_calls] == [
        [*script_module.flox_activate_prefix(script_module.REPO_ROOT), *step.argv]
        for step in steps
    ]

    flox_per_step = [
        (
            argv[len(script_module.flox_activate_prefix(script_module.REPO_ROOT)) :],
            sorted(k for k in cast("dict[str, str]", kwargs["env"]) if k in deploy_env),
        )
        for argv, kwargs in flox_calls
    ]
    # Skip Dagger's git install; compare the recipe steps one-to-one.
    assert flox_per_step == dagger_execs[1:]

    for _argv, kwargs in flox_calls:
        assert kwargs["check"] is True
        assert kwargs["cwd"] == script_module.REPO_ROOT


@pytest.mark.asyncio
async def test_executors_agree_on_the_credential_surface(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same names *and* same values reach ``modal deploy`` on both paths.

    Set-equality on the Dagger side (secrets are the container's whole env
    contribution); subset on the Flox side, where the child also inherits
    PATH/HOME and always will. The honest claim is that the Infisical-derived
    surface is identical, not that the environments are.
    """
    deploy_env = _deploy_env(script_module, host="https://app.infisical.com")

    fake_dagger, chain, _src = _build_dagger_mock()
    with patch.object(script_module, "dagger", fake_dagger):
        await script_module._deploy_via_dagger(HANDLER_FILE, deploy_env=deploy_env)

    dagger_secret_env = {
        cast("str", args[0]): cast("MagicMock", args[1])._secret[1]
        for _step, args in _secret_links(chain)
    }
    assert dagger_secret_env == deploy_env

    flox_calls = _run_flox(script_module, deploy_env, monkeypatch)
    deploy_step_env = cast("dict[str, str]", flox_calls[-1][1]["env"])
    for key, value in deploy_env.items():
        assert deploy_step_env[key] == value

    # And the sync step gets none of them, matching Dagger's late attach.
    sync_step_env = cast("dict[str, str]", flox_calls[0][1]["env"])
    assert [key for key in deploy_env if key in sync_step_env] == []


def test_flox_scrubs_inherited_env_that_would_change_the_artifact(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dict merge cannot express "unset" — these must be removed, not overridden.

    ``TELEMETRY_COLLECTOR_APP=""`` in the operator's shell is the documented
    opt-out: inherited, ``src/secrets_bootstrap.py`` would bake direct-sink
    creds into the app and lose Logfire, while a Dagger deploy of the same
    commit stayed in collector mode. ``MODAL_ENVIRONMENT`` beats
    ``~/.modal.toml`` in modal/config.py, so inheriting it means "same tokens,
    different deploy target". ``UV_*`` is scrubbed as a family — ``UV_NO_DEV=1``
    would strip dev deps out of the synced venv.
    """
    monkeypatch.setenv("TELEMETRY_COLLECTOR_APP", "")
    monkeypatch.setenv("MODAL_ENVIRONMENT", "staging")
    monkeypatch.setenv("HYPERDX_API_KEY", "leaked-provider-cred")
    monkeypatch.setenv("UV_NO_DEV", "1")
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else/.venv")
    monkeypatch.setenv("PATH", "/stub/bin")

    flox_calls = _run_flox(
        script_module,
        _deploy_env(script_module, host=None),
        monkeypatch,
    )

    for _argv, kwargs in flox_calls:
        child_env = cast("dict[str, str]", kwargs["env"])
        for leaked in (
            "TELEMETRY_COLLECTOR_APP",
            "MODAL_ENVIRONMENT",
            "HYPERDX_API_KEY",
            "UV_NO_DEV",
            "VIRTUAL_ENV",
        ):
            assert leaked not in child_env, f"{leaked} reached the Flox child"
        # flox activate needs PATH; scrubbing is targeted, not a whitelist.
        assert child_env["PATH"] == "/stub/bin"
        # The sync must land in a throwaway venv, the Flox counterpart of
        # Dagger's `exclude=[".venv/"]` — `uv sync` prunes, so syncing in
        # place would uninstall extras from the operator's own .venv.
        assert child_env["UV_PROJECT_ENVIRONMENT"] == str(
            script_module.FLOX_DEPLOY_VENV,
        )


def test_scrub_set_covers_every_key_the_bootstrap_secret_reads(
    script_module: ModuleType,
) -> None:
    """Pin the scrub set against ``src/secrets_bootstrap.py``'s actual reads.

    A key added to ``_bootstrap_secret_payload()`` without being added here
    would be silently inherited by the Flox executor and baked into the
    deployed app — the divergence class this whole refactor exists to close.
    """
    from src import secrets_bootstrap

    payload_keys = {
        "INFISICAL_TOKEN",
        "INFISICAL_PROJECT_ID",
        "INFISICAL_HOST",
        "INFISICAL_ENV",
        *secrets_bootstrap._TELEMETRY_POINTER_KEYS,  # pyright: ignore[reportPrivateUsage]
        *secrets_bootstrap._OTEL_SINK_KEYS,  # pyright: ignore[reportPrivateUsage]
    }

    assert payload_keys <= script_module.deploy_env_scrub_keys()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("1", True, id="one"),
        pytest.param("true", True, id="true"),
        pytest.param("TRUE", True, id="uppercase"),
        pytest.param("yes", True, id="yes"),
        pytest.param("on", True, id="on"),
        pytest.param("0", False, id="zero"),
        pytest.param("false", False, id="false"),
        pytest.param("off", False, id="off"),
        pytest.param("", False, id="blank-falls-back-to-default"),
    ],
)
def test_use_flox_accepts_the_documented_spellings(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    *,
    expected: bool,
) -> None:
    """``GTM_DEPLOY_VIA_FLOX=true`` used to silently select *Dagger*."""
    monkeypatch.setenv("GTM_DEPLOY_VIA_FLOX", raw)
    assert script_module._use_flox() is expected


# ---------------------------------------------------------------------------
# Flox preflight
# ---------------------------------------------------------------------------


def _stub_flox_probes(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    results: list[subprocess.CompletedProcess[str]],
    flox_on_path: bool = True,
) -> list[list[str]]:
    """Feed ``_preflight_flox`` canned probe results; capture the argvs it ran."""

    def _which(name: str, *_args: object, **_kwargs: object) -> str | None:
        return "/usr/bin/flox" if (flox_on_path and name == "flox") else None

    monkeypatch.setattr(script_module.shutil, "which", _which)
    seen: list[list[str]] = []
    pending = list(results)

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return pending.pop(0)

    fake_subprocess = MagicMock(name="subprocess")
    fake_subprocess.run.side_effect = _run
    monkeypatch.setattr(script_module, "subprocess", fake_subprocess)
    return seen


def _completed(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_preflight_flox_fails_when_flox_is_absent(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_flox_probes(script_module, monkeypatch, results=[], flox_on_path=False)

    with pytest.raises(SystemExit):
        script_module._preflight_flox()

    assert "flox" in capsys.readouterr().err


def test_preflight_flox_fails_when_activation_fails(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real activation failure is fatal — the toolchain would be unpinned."""
    _stub_flox_probes(script_module, monkeypatch, results=[_completed(1)])

    with pytest.raises(SystemExit):
        script_module._preflight_flox()

    assert "flox activate" in capsys.readouterr().err


def test_preflight_flox_reads_flox_env_and_checks_the_pinned_tools(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``$FLOX_ENV`` comes from flox, and `uv`/`git` are looked for inside it.

    Re-deriving the path (``uname -m | sed s/arm64/aarch64/`` and friends) is
    a second implementation of something the activation already exports.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").touch()
    (bin_dir / "git").touch()

    seen = _stub_flox_probes(
        script_module,
        monkeypatch,
        results=[_completed(0, str(tmp_path))],
    )
    script_module._preflight_flox()

    assert len(seen) == 1
    assert seen[0] == [
        *script_module.flox_activate_prefix(script_module.REPO_ROOT),
        "sh",
        "-c",
        'printf %s "$FLOX_ENV"',
    ]
    assert "uv, git pinned" in capsys.readouterr().out


def test_preflight_flox_asks_the_activation_when_tools_are_not_in_flox_env_bin(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Flox composes several store paths, so `$FLOX_ENV/bin` is not exhaustive.

    The fallback must ask the *activated* shell — this process's own
    ``shutil.which`` cannot see inside an activation.
    """
    seen = _stub_flox_probes(
        script_module,
        monkeypatch,
        results=[_completed(0, str(tmp_path)), _completed(0, "/nix/store/…/uv\n")],
    )
    script_module._preflight_flox()

    assert seen[1] == [
        *script_module.flox_activate_prefix(script_module.REPO_ROOT),
        "sh",
        "-c",
        "command -v uv && command -v git",
    ]


def test_preflight_flox_tolerates_an_activation_that_reports_no_flox_env(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """rc=0 with an empty FLOX_ENV means "not really flox" — say so, don't lie.

    A genuinely failed activation already exited above, so the only casualty
    is the pinning guarantee. The deploy still works; the preflight must not
    claim a check it could not perform.
    """
    _stub_flox_probes(script_module, monkeypatch, results=[_completed(0, "")])

    script_module._preflight_flox()

    assert "unverified" in capsys.readouterr().out


def test_preflight_flox_never_invokes_flox_without_a_double_dash(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`flox --version` and friends are useless as probes.

    The test suite's pass-through `flox` stub shifts until it finds `--`;
    with no `--` it shifts an empty `$@` and execs nothing, returning 0. Any
    availability probe built that way passes unconditionally, including on a
    machine with no flox at all.
    """
    seen = _stub_flox_probes(
        script_module,
        monkeypatch,
        results=[_completed(0, str(tmp_path)), _completed(0, "/bin/uv")],
    )
    script_module._preflight_flox()

    for argv in seen:
        assert "--" in argv, f"probe without a `--` separator: {argv}"


def test_use_flox_rejects_a_bogus_value(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GTM_DEPLOY_VIA_FLOX", "flox-please")
    with pytest.raises(SystemExit) as excinfo:
        script_module._use_flox()

    assert excinfo.value.code == 1
    assert "GTM_DEPLOY_VIA_FLOX" in capsys.readouterr().err
