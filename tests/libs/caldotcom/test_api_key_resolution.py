from __future__ import annotations

import pytest

from libs.caldotcom.client import CalcomClient, api_key_scope


def test_from_env_prefers_scope_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CALCOM_API_KEY", "from-env")
    with api_key_scope("from-scope"):
        client = CalcomClient.from_env()
    # Auth is attached per-request now, not baked into client params; the
    # resolved key is stored on the client.
    assert client._api_key == "from-scope"  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    client.close()


def test_from_env_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CALCOM_API_KEY", "from-env")
    client = CalcomClient.from_env()
    assert client._api_key == "from-env"  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    client.close()


def test_from_env_raises_when_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CALCOM_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CALCOM_API_KEY not resolved"):
        CalcomClient.from_env()


def test_api_key_scope_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALCOM_API_KEY", raising=False)
    with api_key_scope("inside"):
        c = CalcomClient.from_env()
        assert c._api_key == "inside"  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        c.close()
    with pytest.raises(RuntimeError, match="CALCOM_API_KEY not resolved"):
        CalcomClient.from_env()
