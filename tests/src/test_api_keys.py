from __future__ import annotations

import os

from src.api_keys import inject_api_keys


def test_inject_sets_env_vars():
    os.environ.pop("ATTIO_API_KEY", None)
    with inject_api_keys({"attio_api_key": "sk-test"}):
        assert os.environ["ATTIO_API_KEY"] == "sk-test"
    assert "ATTIO_API_KEY" not in os.environ


def test_inject_restores_original():
    os.environ["ATTIO_API_KEY"] = "sk-original"
    with inject_api_keys({"attio_api_key": "sk-override"}):
        assert os.environ["ATTIO_API_KEY"] == "sk-override"
    assert os.environ["ATTIO_API_KEY"] == "sk-original"
    os.environ.pop("ATTIO_API_KEY", None)


def test_inject_empty_dict_is_noop():
    os.environ.pop("ATTIO_API_KEY", None)
    with inject_api_keys({}):
        assert "ATTIO_API_KEY" not in os.environ


def test_inject_multiple_keys():
    os.environ.pop("ATTIO_API_KEY", None)
    os.environ.pop("PARALLEL_API_KEY", None)
    with inject_api_keys({"attio_api_key": "sk-a", "parallel_api_key": "sk-p"}):
        assert os.environ["ATTIO_API_KEY"] == "sk-a"
        assert os.environ["PARALLEL_API_KEY"] == "sk-p"
    assert "ATTIO_API_KEY" not in os.environ
    assert "PARALLEL_API_KEY" not in os.environ


def test_inject_restores_on_exception():
    os.environ["ATTIO_API_KEY"] = "sk-original"
    try:
        with inject_api_keys({"attio_api_key": "sk-boom"}):
            raise ValueError("boom")
    except ValueError:
        pass
    assert os.environ["ATTIO_API_KEY"] == "sk-original"
    os.environ.pop("ATTIO_API_KEY", None)


def test_inject_rejects_unknown_keys_without_mutation():
    os.environ["ATTIO_API_KEY"] = "sk-original"

    try:
        with inject_api_keys({"attio_api_key": "sk-temp", "unknown_key": "secret"}):
            raise AssertionError("context manager should not yield with invalid keys")
    except ValueError as exc:
        assert "unknown_key" in str(exc)

    assert os.environ["ATTIO_API_KEY"] == "sk-original"
    os.environ.pop("ATTIO_API_KEY", None)
