from __future__ import annotations

# ruff: noqa: PLR2004, S101, SLF001 -- white-box tests intentionally use
# assertions and exercise the probe's private bootstrap sentinel.

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, Self

import pytest

from scripts.lib import env as env_lib

if TYPE_CHECKING:
    from collections.abc import Callable


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "attio-workspace_slug-probe.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "attio_workspace_slug_probe",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_probe(
    *,
    returns: str | None = None,
    raises: BaseException | None = None,
    calls: list[tuple[str, bool]] | None = None,
) -> Callable[..., str]:
    """Build a stand-in for the module-level `probe`.

    Tests patch `probe` by name rather than an async driver, and the stub
    takes `api_key` / `json_output` as required keywords so that a `main()`
    which stopped forwarding either one fails here instead of passing
    against a permissive `**kwargs` signature.
    """

    def _probe(*, api_key: str, json_output: bool) -> str:
        if calls is not None:
            calls.append((api_key, json_output))
        if raises is not None:
            raise raises
        assert returns is not None
        return returns

    return _probe


@pytest.fixture(autouse=True)
def _scrub_bootstrap_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """The bootstrap sentinel is set on `os.environ` directly inside the
    script (so it survives `execvp`). In tests where `execvp` is monkeypatched
    away, that side effect leaks across tests and short-circuits subsequent
    bootstraps. Scrub it before every test."""
    module = _load_script_module()
    monkeypatch.delenv(module._BOOTSTRAP_SENTINEL_ENV, raising=False)


def test_missing_creds_shows_canonical_infisical_invocation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.delenv("INFISICAL_PROJECT_ID", raising=False)
    monkeypatch.delenv("INFISICAL_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "prod"])
    # The `.env.local` fallback now lives in scripts/lib/env, so point *that*
    # module at an empty directory — patching the script's own REPO_ROOT would
    # silently stop covering anything and the test would pass on a machine
    # with real credentials on disk.
    monkeypatch.setattr(env_lib, "REPO_ROOT", tmp_path)

    module = _load_script_module()

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert (
        'infisical run --projectId "$INFISICAL_PROJECT_ID" '
        '--token "$INFISICAL_TOKEN" --env=<dev|prod> -- '
        "scripts/attio-workspace_slug-probe.py"
    ) in captured.err


def test_missing_env_refuses_to_default_to_prod(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No silent prod default: refuse to bootstrap when --env and
    INFISICAL_ENV are both unset (codex review finding — silently probing
    prod returns the wrong workspace slug)."""
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.delenv("INFISICAL_ENV", raising=False)
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH)])

    module = _load_script_module()
    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Infisical environment is required" in captured.err
    assert "INFISICAL_ENV" in captured.err


def test_preinjected_api_key_does_not_require_env_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If ATTIO_API_KEY is already in the environment (e.g. exported manually
    or from another secret manager), --env / INFISICAL_ENV are unnecessary —
    the script should run the probe directly (codex review finding)."""
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.delenv("INFISICAL_ENV", raising=False)
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH)])

    module = _load_script_module()
    monkeypatch.setattr(module, "probe", _stub_probe(returns="acme"))

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "acme\n"
    assert captured.err == ""


def test_infisical_env_env_var_is_honored_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INFISICAL_ENV stands in for --env, mirroring the repo convention
    (see gtm-sdk/AGENTS.md: `export INFISICAL_ENV=dev` — explicit; no default)."""
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "proj-xyz")
    monkeypatch.setenv("INFISICAL_TOKEN", "tok-abc")
    monkeypatch.setenv("INFISICAL_ENV", "dev")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH)])

    captured_argv: list[list[str]] = []

    def fake_execvp(file: str, argv: list[str]) -> None:
        captured_argv.append([file, *argv])

    module = _load_script_module()
    monkeypatch.setattr(module.os, "execvp", fake_execvp)

    module.main()

    assert len(captured_argv) == 1
    invocation = captured_argv[0]
    assert "--env=dev" in invocation


def test_explicit_env_prod_flag_self_bootstraps_via_infisical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "proj-xyz")
    monkeypatch.setenv("INFISICAL_TOKEN", "tok-abc")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "prod"])

    captured_argv: list[list[str]] = []

    def fake_execvp(file: str, argv: list[str]) -> None:
        captured_argv.append([file, *argv])

    module = _load_script_module()
    monkeypatch.setattr(module.os, "execvp", fake_execvp)

    module.main()

    assert len(captured_argv) == 1
    invocation = captured_argv[0]
    assert invocation[0] == "infisical"
    assert invocation[1:7] == [
        "infisical",
        "run",
        "--projectId",
        "proj-xyz",
        "--token",
        "tok-abc",
    ]
    assert "--env=prod" in invocation
    assert str(SCRIPT_PATH) in invocation


def test_whitespace_in_env_credentials_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing newlines on copy-pasted credentials must not leak through to
    Attio (codex review finding — `Bearer key\\n` returns a 401 that looks
    identical to a bad key)."""
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "  proj-xyz\n")
    monkeypatch.setenv("INFISICAL_TOKEN", "tok-abc\n")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "dev"])

    captured_argv: list[list[str]] = []

    def fake_execvp(file: str, argv: list[str]) -> None:
        captured_argv.append([file, *argv])

    module = _load_script_module()
    monkeypatch.setattr(module.os, "execvp", fake_execvp)

    module.main()

    invocation = captured_argv[0]
    project_idx = invocation.index("--projectId")
    token_idx = invocation.index("--token")
    assert invocation[project_idx + 1] == "proj-xyz"
    assert invocation[token_idx + 1] == "tok-abc"


def test_explicit_env_dev_flag_is_forwarded_through_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "proj-xyz")
    monkeypatch.setenv("INFISICAL_TOKEN", "tok-abc")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "dev", "--json"])

    captured_argv: list[list[str]] = []
    module = _load_script_module()

    def fake_execvp(file: str, argv: list[str]) -> None:
        captured_argv.append([file, *argv])

    monkeypatch.setattr(module.os, "execvp", fake_execvp)

    module.main()

    invocation = captured_argv[0]
    assert "--env=dev" in invocation
    script_idx = invocation.index(str(SCRIPT_PATH))
    forwarded = invocation[script_idx + 1 :]
    assert "--env=dev" in forwarded
    assert "--json" in forwarded


def test_extract_workspace_slug_active_token() -> None:
    module = _load_script_module()
    body = json.dumps(
        {
            "active": True,
            "scope": "record_permission:read",
            "token_type": "Bearer",  # nosec B105 -- /v2/self response field, not a credential
            "workspace_id": "00000000-0000-0000-0000-000000000000",
            "workspace_name": "Acme",
            "workspace_slug": "acme",
        },
    )

    assert module.extract_workspace_slug(body) == "acme"


def test_extract_workspace_slug_inactive_token_raises() -> None:
    module = _load_script_module()
    body = json.dumps({"active": False})

    with pytest.raises(ValueError, match="workspace_slug"):
        module.extract_workspace_slug(body)


def test_extract_workspace_slug_empty_slug_raises() -> None:
    module = _load_script_module()
    body = json.dumps({"active": True, "workspace_slug": ""})

    with pytest.raises(ValueError, match="workspace_slug"):
        module.extract_workspace_slug(body)


@pytest.mark.parametrize("non_dict_payload", ["null", "[]", '"a string"', "42"])
def test_extract_workspace_slug_non_object_payload_raises(
    non_dict_payload: str,
) -> None:
    """Guard against proxy or future-API responses where /v2/self is JSON but
    not a dict — a bare `.get()` would AttributeError and escape main()'s
    catch (codex review finding)."""
    module = _load_script_module()

    with pytest.raises(ValueError, match="not a JSON object"):
        module.extract_workspace_slug(non_dict_payload)


@pytest.mark.parametrize(
    "invalid_body",
    [
        "<html><body>502 Bad Gateway</body></html>",
        "not json at all",
        "{",  # truncated
    ],
)
def test_extract_workspace_slug_invalid_json_raises_value_error(
    invalid_body: str,
) -> None:
    """A non-JSON 200 (e.g. an upstream proxy HTML page) must surface as
    ValueError so main()'s clean stderr path catches it (codex review)."""
    module = _load_script_module()

    with pytest.raises(ValueError, match="not valid JSON"):
        module.extract_workspace_slug(invalid_body)


def test_main_happy_path_prints_slug_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path: probe() returns the slug, main() prints it with a
    trailing newline to stdout and exits zero."""
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "dev"])

    module = _load_script_module()
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(module, "probe", _stub_probe(returns="acme", calls=calls))

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "acme\n"
    assert captured.err == ""
    assert calls == [("test-token-not-real", False)]


def test_main_happy_path_json_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --json, probe() returns a pretty-printed JSON string and main()
    writes it to stdout verbatim (newline-terminated)."""
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "dev", "--json"])

    module = _load_script_module()
    pretty = json.dumps(
        {"active": True, "workspace_slug": "acme"},
        indent=2,
    )
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(module, "probe", _stub_probe(returns=pretty, calls=calls))

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == pretty + "\n"
    assert captured.err == ""
    # `--json` has to reach probe(): it selects which of the two renderings
    # the single round trip produces.
    assert calls == [("test-token-not-real", True)]


def test_bootstrap_sentinel_blocks_infinite_loop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If the Infisical env doesn't contain ATTIO_API_KEY, fail fast rather
    than re-execing `infisical run` forever (codex review finding)."""
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "proj-xyz")
    monkeypatch.setenv("INFISICAL_TOKEN", "tok-abc")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "dev"])

    module = _load_script_module()
    monkeypatch.setenv(module._BOOTSTRAP_SENTINEL_ENV, "1")

    def fail_if_execvp_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "execvp must NOT be called once the bootstrap sentinel is set",
        )

    monkeypatch.setattr(module.os, "execvp", fail_if_execvp_called)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "ATTIO_API_KEY is not present in the Infisical 'dev' environment" in (
        captured.err
    )


def test_probe_failure_surfaces_attio_error_body_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bad Attio response must not dump a traceback — surface a clean
    stderr message and exit non-zero (codex review finding)."""
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "dev"])

    module = _load_script_module()
    monkeypatch.setattr(
        module,
        "probe",
        _stub_probe(
            raises=module.AttioProbeError(
                "/v2/self request failed: 401 unauthorized",
            ),
        ),
    )

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert (
        "attio probe failed: /v2/self request failed: 401 unauthorized" in captured.err
    )
    assert captured.out == ""


def test_probe_inactive_token_surfaces_value_error_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An inactive token returns 200 with `{"active": false}` (no slug).
    extract_workspace_slug raises ValueError; main() must catch it cleanly."""
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "dev"])

    module = _load_script_module()
    monkeypatch.setattr(
        module,
        "probe",
        _stub_probe(
            raises=ValueError(
                "/v2/self response did not include a workspace_slug: {'active': False}",
            ),
        ),
    )

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "attio probe failed:" in captured.err
    assert "workspace_slug" in captured.err


class _FakeIdentity:
    """Stands in for the SDK's /v2/self response model.

    Only `model_dump` matters: `model_dump_or_empty` duck-types on it, so a
    real pydantic model buys the test nothing.
    """

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, object]:
        return dict(self._payload)


class _FakeSdk:
    """Records the round trips `probe` makes through the Attio SDK."""

    def __init__(
        self,
        *,
        identity: object = None,
        raises: BaseException | None = None,
        call_count: list[int] | None = None,
    ) -> None:
        self._identity = identity
        self._raises = raises
        self._call_count = call_count
        self.meta = self

    def get_v2_self(self) -> object:
        if self._call_count is not None:
            self._call_count.append(1)
        if self._raises is not None:
            raise self._raises
        return self._identity

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    sdk: _FakeSdk,
    keys_seen: list[str | None] | None = None,
) -> None:
    def fake_get_client(api_key: str | None = None) -> _FakeSdk:
        if keys_seen is not None:
            keys_seen.append(api_key)
        return sdk

    monkeypatch.setattr(module, "get_client", fake_get_client)


_SELF_PAYLOAD: dict[str, object] = {
    "active": True,
    "scope": "record_permission:read",
    "workspace_id": "00000000-0000-0000-0000-000000000000",
    "workspace_name": "Acme",
    "workspace_slug": "acme",
}


def test_probe_returns_the_slug_and_forwards_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script_module()
    calls: list[int] = []
    keys_seen: list[str | None] = []
    _patch_client(
        monkeypatch,
        module,
        _FakeSdk(identity=_FakeIdentity(_SELF_PAYLOAD), call_count=calls),
        keys_seen,
    )

    assert module.probe(api_key="key-alpha", json_output=False) == "acme"
    # The key must reach get_client explicitly rather than leaking in via
    # os.environ, which is what the bootstrap re-exec populates.
    assert keys_seen == ["key-alpha"]
    assert len(calls) == 1


def test_probe_json_mode_serves_the_same_single_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--json` changes the rendering, not the number of API calls.

    The Dagger container issued exactly one `GET /v2/self` for both modes;
    a second call here would be a behaviour change (and a second chance to
    observe a different workspace).
    """
    module = _load_script_module()
    calls: list[int] = []
    _patch_client(
        monkeypatch,
        module,
        _FakeSdk(identity=_FakeIdentity(_SELF_PAYLOAD), call_count=calls),
    )

    output = module.probe(api_key="key-alpha", json_output=True)

    assert json.loads(output) == _SELF_PAYLOAD
    assert len(calls) == 1
    # The two modes agree on the payload they were derived from.
    assert module.extract_workspace_slug(output) == "acme"


def test_probe_maps_an_attio_error_envelope_to_code_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attio's real code/message must survive, not pydantic's Literal noise.

    The SDK's `Code` Literal omits most real codes, so the SDK raises a
    validation error whose str() hides the cause; `describe_attio_error`
    re-parses `.body` to recover it.
    """
    module = _load_script_module()

    class _SdkError(Exception):
        body = json.dumps(
            {
                "status_code": 401,
                "type": "authentication_error",
                "code": "unauthorized",
                "message": "Invalid API key",
            },
        )

    _patch_client(monkeypatch, module, _FakeSdk(raises=_SdkError("noise")))

    with pytest.raises(module.AttioProbeError) as excinfo:
        module.probe(api_key="key-alpha", json_output=False)

    message = str(excinfo.value)
    assert "unauthorized" in message
    assert "Invalid API key" in message


def test_probe_falls_back_to_str_for_a_non_envelope_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport error has no Attio envelope; still one clean message."""
    module = _load_script_module()
    _patch_client(
        monkeypatch,
        module,
        _FakeSdk(raises=ConnectionError("connection reset")),
    )

    with pytest.raises(module.AttioProbeError, match="connection reset"):
        module.probe(api_key="key-alpha", json_output=False)


def test_probe_rejects_a_response_that_carries_no_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inactive token 200s without a slug: ValueError, not a bare crash.

    `model_dump_or_empty` also yields `{}` for a response the SDK returns as
    something other than a model, and that lands on the same branch.
    """
    module = _load_script_module()
    _patch_client(
        monkeypatch,
        module,
        _FakeSdk(identity=_FakeIdentity({"active": False})),
    )

    with pytest.raises(ValueError, match="workspace_slug"):
        module.probe(api_key="key-alpha", json_output=False)


def test_probe_no_longer_depends_on_dagger_or_asyncio() -> None:
    """The container path is gone, not merely bypassed.

    Without this, deleting the call sites while leaving `import dagger` (and
    the engine startup cost, plus the scrypt cache-tag it needed) in place
    would look identical to every other test in this file.
    """
    module = _load_script_module()

    assert not hasattr(module, "asyncio")
    assert not hasattr(module, "dagger")
    assert not hasattr(module, "PROBE_SCRIPT")
    assert not hasattr(module, "_cache_key_tag")
    # The private env helpers moved to scripts/lib/env.py, where a single
    # implementation is covered by tests/scripts/test_env_helpers.py.
    assert not hasattr(module, "_parse_dotenv")
    assert not hasattr(module, "_clean_env")
    assert not hasattr(module, "_read_infisical_credentials")
