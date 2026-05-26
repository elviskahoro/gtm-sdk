from __future__ import annotations

import importlib
from types import ModuleType

import pytest


class _FakeAttioSDK:
    """Captures constructor kwargs so tests can assert what get_client passes."""

    last_kwargs: dict[str, object] = {}

    def __init__(self, **kwargs: object) -> None:
        _FakeAttioSDK.last_kwargs = kwargs


def _reload_client(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Re-import libs.attio.client so module-level ATTIO_OP_TIMEOUT_SECONDS picks up env."""
    import libs.attio.client as client_module

    monkeypatch.setattr(
        client_module,
        "get_attio_sdk_client_class",
        lambda: _FakeAttioSDK,
    )
    reloaded = importlib.reload(client_module)
    monkeypatch.setattr(reloaded, "get_attio_sdk_client_class", lambda: _FakeAttioSDK)
    return reloaded


def test_get_client_passes_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    monkeypatch.delenv("ATTIO_OP_TIMEOUT_SECONDS", raising=False)
    module = _reload_client(monkeypatch)

    module.get_client()

    assert _FakeAttioSDK.last_kwargs["oauth2"] == "test-token"
    assert _FakeAttioSDK.last_kwargs["timeout_ms"] == 10_000


def test_get_client_respects_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    monkeypatch.setenv("ATTIO_OP_TIMEOUT_SECONDS", "2.5")
    module = _reload_client(monkeypatch)

    module.get_client()

    assert _FakeAttioSDK.last_kwargs["timeout_ms"] == 2500


def test_get_client_raises_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    module = _reload_client(monkeypatch)

    from libs.attio.errors import AttioAuthError

    with pytest.raises(AttioAuthError):
        module.get_client()


# --- ai-2aw: api_key arg + api_key_scope context (B-contextvar) ---


class _CapturingFakeAttio:
    """Light stand-in that records the oauth2 key get_client passed."""

    last_oauth2: str | None = None

    def __init__(self, *, oauth2: str, timeout_ms: int, **_kwargs: object) -> None:
        type(self).last_oauth2 = oauth2


@pytest.fixture
def _fake_sdk(  # pyright: ignore[reportUnusedFunction]  # consumed by tests as a fixture
    monkeypatch: pytest.MonkeyPatch,
) -> type[_CapturingFakeAttio]:
    _CapturingFakeAttio.last_oauth2 = None
    import libs.attio.client as client_module

    monkeypatch.setattr(
        client_module,
        "get_attio_sdk_client_class",
        lambda: _CapturingFakeAttio,
    )
    return _CapturingFakeAttio


def test_get_client_uses_explicit_api_key_over_everything(
    monkeypatch: pytest.MonkeyPatch,
    _fake_sdk: type[_CapturingFakeAttio],
) -> None:
    from libs.attio.client import api_key_scope, get_client

    monkeypatch.setenv("ATTIO_API_KEY", "from-env")
    with api_key_scope("from-scope"):
        get_client(api_key="from-arg")
    assert _fake_sdk.last_oauth2 == "from-arg"


def test_get_client_uses_scope_over_env(
    monkeypatch: pytest.MonkeyPatch,
    _fake_sdk: type[_CapturingFakeAttio],
) -> None:
    from libs.attio.client import api_key_scope, get_client

    monkeypatch.setenv("ATTIO_API_KEY", "from-env")
    with api_key_scope("from-scope"):
        get_client()
    assert _fake_sdk.last_oauth2 == "from-scope"


def test_api_key_scope_resets_on_exit(
    monkeypatch: pytest.MonkeyPatch,
    _fake_sdk: type[_CapturingFakeAttio],
) -> None:
    from libs.attio.client import api_key_scope, get_client
    from libs.attio.errors import AttioAuthError

    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    with api_key_scope("inside"):
        get_client()
        assert _fake_sdk.last_oauth2 == "inside"
    with pytest.raises(AttioAuthError):
        get_client()


def test_api_key_scope_nests(
    monkeypatch: pytest.MonkeyPatch,
    _fake_sdk: type[_CapturingFakeAttio],
) -> None:
    from libs.attio.client import api_key_scope, get_client

    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    with api_key_scope("outer"):
        get_client()
        assert _fake_sdk.last_oauth2 == "outer"
        with api_key_scope("inner"):
            get_client()
            assert _fake_sdk.last_oauth2 == "inner"
        get_client()
        assert _fake_sdk.last_oauth2 == "outer"
