from __future__ import annotations

import pytest

from libs import infisical
from libs.infisical import InfisicalAuthError, InfisicalFetchError
from libs.infisical import client as infisical_client


def test_fetch_returns_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "from-env")
    with infisical.fetch("ATTIO_API_KEY") as key:
        assert key == "from-env"


def test_fetch_strips_env_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "  spaced-key  ")
    with infisical.fetch("ATTIO_API_KEY") as key:
        assert key == "spaced-key"


def test_fetch_falls_back_to_infisical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.setenv("INFISICAL_TOKEN", "fake-token")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "fake-project")
    monkeypatch.setenv("INFISICAL_ENV", "dev")

    captured: dict[str, str] = {}

    def fake_fetch(name: str) -> str:
        captured["name"] = name
        return "from-infisical"

    monkeypatch.setattr(infisical_client, "_fetch_from_infisical", fake_fetch)

    with infisical.fetch("ATTIO_API_KEY") as key:
        assert key == "from-infisical"
    assert captured["name"] == "ATTIO_API_KEY"


def test_fetch_raises_when_bootstrap_creds_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.delenv("INFISICAL_TOKEN", raising=False)
    monkeypatch.delenv("INFISICAL_PROJECT_ID", raising=False)
    with pytest.raises(InfisicalAuthError):
        with infisical.fetch("ATTIO_API_KEY"):
            pass


def test_fetch_raises_when_infisical_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetch must fail closed if INFISICAL_ENV is missing.

    Defaulting to ``dev`` would silently route prod traffic to dev secrets
    when an operator forgets to ``export INFISICAL_ENV=prod`` (see ai-2aw).
    """
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.setenv("INFISICAL_TOKEN", "fake-token")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "fake-project")
    monkeypatch.delenv("INFISICAL_ENV", raising=False)
    with pytest.raises(InfisicalAuthError, match="INFISICAL_ENV"):
        with infisical.fetch("ATTIO_API_KEY"):
            pass


def test_fetch_raises_when_infisical_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    monkeypatch.setenv("INFISICAL_TOKEN", "fake-token")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "fake-project")

    def _empty(_name: str) -> str:
        return ""

    monkeypatch.setattr(infisical_client, "_fetch_from_infisical", _empty)
    with pytest.raises(InfisicalFetchError):
        with infisical.fetch("ATTIO_API_KEY"):
            pass


def test_fetch_all_resolves_each_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A", "1")
    monkeypatch.setenv("B", "2")
    with infisical.fetch_all(["A", "B"]) as resolved:
        assert resolved == {"A": "1", "B": "2"}


def test_fetch_all_mixed_env_and_infisical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FROM_ENV", "env-value")
    monkeypatch.delenv("FROM_INFISICAL", raising=False)
    monkeypatch.setenv("INFISICAL_TOKEN", "fake-token")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "fake-project")

    def _by_name(name: str) -> str:
        return f"infisical-{name.lower()}"

    monkeypatch.setattr(infisical_client, "_fetch_from_infisical", _by_name)
    with infisical.fetch_all(["FROM_ENV", "FROM_INFISICAL"]) as resolved:
        assert resolved == {
            "FROM_ENV": "env-value",
            "FROM_INFISICAL": "infisical-from_infisical",
        }
