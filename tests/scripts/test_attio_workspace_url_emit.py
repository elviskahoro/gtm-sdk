from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "attio-workspace_url-emit.py"
)

_SLUG = "acme"
_RECORD_ID = "bf071e1f-6035-429d-b874-d83ea64ea13b"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "attio_workspace_url_emit",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Pure URL builders (no network) ----------------------------------------


def test_build_workspace_url() -> None:
    module = _load_script_module()
    assert module.build_workspace_url(_SLUG) == "https://app.attio.com/acme"


def test_build_object_list_url_uses_plural_and_view() -> None:
    module = _load_script_module()
    assert (
        module.build_object_list_url(_SLUG, "companies")
        == "https://app.attio.com/acme/companies/view/"
    )


def test_build_record_url_maps_standard_plural_to_singular() -> None:
    module = _load_script_module()
    assert (
        module.build_record_url(_SLUG, "people", _RECORD_ID)
        == f"https://app.attio.com/acme/person/{_RECORD_ID}"
    )


def test_build_record_url_custom_object_falls_back_to_trailing_s_strip() -> None:
    module = _load_script_module()
    assert (
        module.build_record_url(_SLUG, "widgets", _RECORD_ID)
        == f"https://app.attio.com/acme/widget/{_RECORD_ID}"
    )


def test_build_record_url_custom_object_without_trailing_s_left_as_is() -> None:
    module = _load_script_module()
    assert (
        module.build_record_url(_SLUG, "sheep", _RECORD_ID)
        == f"https://app.attio.com/acme/sheep/{_RECORD_ID}"
    )


def test_build_standard_object_urls_covers_all_standard_objects() -> None:
    module = _load_script_module()
    urls = module.build_standard_object_urls(_SLUG)
    assert set(urls) == set(module.STANDARD_OBJECTS_PLURAL_TO_SINGULAR)
    assert urls["companies"] == "https://app.attio.com/acme/companies/view/"


# --- Argument validation & auth guard --------------------------------------


def test_record_id_without_object_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--record-id", _RECORD_ID])

    module = _load_script_module()
    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--record-id requires --object" in captured.err


def test_missing_api_key_shows_canonical_infisical_invocation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH)])

    module = _load_script_module()
    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert (
        'infisical run --projectId "$INFISICAL_PROJECT_ID" '
        '--token "$INFISICAL_TOKEN" --env=<dev|prod> -- '
        "scripts/attio-workspace_url-emit.py"
    ) in captured.err


# --- Happy paths (fetch_token_scopes patched) ------------------------------


def _patch_active_slug(
    module,
    monkeypatch: pytest.MonkeyPatch,
    *,
    active: bool = True,
    slug: str = _SLUG,
) -> None:
    def _fake_fetch() -> tuple[bool, set[str], str]:
        return active, set[str](), slug

    monkeypatch.setattr(module, "fetch_token_scopes", _fake_fetch)


def test_default_output_prints_base_and_object_links(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH)])

    module = _load_script_module()
    _patch_active_slug(module, monkeypatch)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    lines = captured.out.splitlines()
    assert lines[0] == "https://app.attio.com/acme"
    assert "companies: https://app.attio.com/acme/companies/view/" in lines
    assert "people: https://app.attio.com/acme/people/view/" in lines
    assert captured.err == ""


def test_object_flag_prints_single_list_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--object", "companies"])

    module = _load_script_module()
    _patch_active_slug(module, monkeypatch)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "https://app.attio.com/acme/companies/view/\n"


def test_object_and_record_id_prints_singular_record_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr(
        "sys.argv",
        [str(SCRIPT_PATH), "--object", "people", "--record-id", _RECORD_ID],
    )

    module = _load_script_module()
    _patch_active_slug(module, monkeypatch)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"https://app.attio.com/acme/person/{_RECORD_ID}\n"


def test_json_default_output_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--json"])

    module = _load_script_module()
    _patch_active_slug(module, monkeypatch)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["workspace_slug"] == "acme"
    assert payload["base_url"] == "https://app.attio.com/acme"
    assert payload["objects"]["people"] == "https://app.attio.com/acme/people/view/"


def test_json_object_output_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr(
        "sys.argv",
        [str(SCRIPT_PATH), "--object", "companies", "--json"],
    )

    module = _load_script_module()
    _patch_active_slug(module, monkeypatch)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload == {
        "workspace_slug": "acme",
        "url": "https://app.attio.com/acme/companies/view/",
    }


# --- Failure surfaces -------------------------------------------------------


def test_inactive_token_surfaces_clean_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH)])

    module = _load_script_module()
    _patch_active_slug(module, monkeypatch, active=False, slug="")

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "attio workspace-url failed:" in captured.err
    assert "inactive" in captured.err
    assert captured.out == ""


def test_empty_slug_surfaces_clean_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token-not-real")
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH)])

    module = _load_script_module()
    _patch_active_slug(module, monkeypatch, active=True, slug="")

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "attio workspace-url failed:" in captured.err
    assert "workspace_slug" in captured.err
    assert captured.out == ""
