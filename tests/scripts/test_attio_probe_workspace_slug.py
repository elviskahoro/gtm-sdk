from __future__ import annotations

import importlib.util
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "attio-probe_workspace_slug.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "attio_probe_workspace_slug",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    tmp_path: pytest.TempPathFactory,
) -> None:
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.delenv("INFISICAL_PROJECT_ID", raising=False)
    monkeypatch.delenv("INFISICAL_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "prod"])

    module = _load_script_module()
    monkeypatch.setattr(module, "REPO_ROOT", Path(str(tmp_path)))

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert (
        'infisical run --projectId "$INFISICAL_PROJECT_ID" '
        '--token "$INFISICAL_TOKEN" --env=<dev|prod> -- '
        "scripts/attio-probe_workspace_slug.py"
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

    def fake_asyncio_run(coro: Coroutine[Any, Any, str]) -> str:
        coro.close()
        return "acme"

    monkeypatch.setattr(module.asyncio, "run", fake_asyncio_run)

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


def test_parse_dotenv_handles_export_quotes_and_inline_comments() -> None:
    """`.env.local` files in the wild use `export KEY=value`, quoted values,
    and inline comments — the parser must accept all three (codex review
    finding)."""
    module = _load_script_module()
    text = "\n".join(
        [
            "# top-level comment",
            "",
            "INFISICAL_PROJECT_ID=plain-value",
            'INFISICAL_TOKEN="quoted-value"',
            "export ALT_PROJECT_ID=exported  # trailing comment",
            "export ALT_TOKEN='single-quoted'",
            "BLANK=",
            "  # indented comment",
        ],
    )
    parsed = module._parse_dotenv(text)

    assert parsed["INFISICAL_PROJECT_ID"] == "plain-value"
    assert parsed["INFISICAL_TOKEN"] == "quoted-value"
    assert parsed["ALT_PROJECT_ID"] == "exported"
    assert parsed["ALT_TOKEN"] == "single-quoted"
    assert parsed["BLANK"] == ""


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
    """Happy-path Dagger flow: probe() returns the slug, main() prints it
    with a trailing newline to stdout and exits zero."""
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "dev"])

    module = _load_script_module()

    def fake_asyncio_run(coro: Coroutine[Any, Any, str]) -> str:
        coro.close()
        return "acme"

    monkeypatch.setattr(module.asyncio, "run", fake_asyncio_run)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "acme\n"
    assert captured.err == ""


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

    def fake_asyncio_run(coro: Coroutine[Any, Any, str]) -> str:
        coro.close()
        return pretty

    monkeypatch.setattr(module.asyncio, "run", fake_asyncio_run)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == pretty + "\n"
    assert captured.err == ""


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
    """A failed Dagger exec / bad Attio response must not dump a traceback —
    surface a clean stderr message and exit non-zero (codex review finding)."""
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--env", "dev"])

    module = _load_script_module()

    def fake_asyncio_run(coro: Coroutine[Any, Any, str]) -> str:
        # Close the unawaited coroutine to suppress RuntimeWarning.
        coro.close()
        raise module.AttioProbeError("/v2/self request failed: 401 unauthorized")

    monkeypatch.setattr(module.asyncio, "run", fake_asyncio_run)

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

    def fake_asyncio_run(coro: Coroutine[Any, Any, str]) -> str:
        # Close the unawaited coroutine to suppress RuntimeWarning.
        coro.close()
        raise ValueError(
            "/v2/self response did not include a workspace_slug: {'active': False}",
        )

    monkeypatch.setattr(module.asyncio, "run", fake_asyncio_run)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "attio probe failed:" in captured.err
    assert "workspace_slug" in captured.err
