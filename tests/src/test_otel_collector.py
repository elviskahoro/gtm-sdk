from __future__ import annotations

from typing import Any

import src.otel_collector as otel_collector
from src.otel_collector import (  # exercising collector internals directly
    _GRAFANA_DEFAULT_ENDPOINT,  # trunk-ignore(pyright/reportPrivateUsage)
    _PROVIDER_SECRET_KEYS,  # trunk-ignore(pyright/reportPrivateUsage)
    _base_endpoint,  # trunk-ignore(pyright/reportPrivateUsage)
    _collector_secret_payload,  # trunk-ignore(pyright/reportPrivateUsage)
    _ensure_otelcol_running,  # trunk-ignore(pyright/reportPrivateUsage)
    _grafana_basic_auth,  # trunk-ignore(pyright/reportPrivateUsage)
    _post_local,  # trunk-ignore(pyright/reportPrivateUsage)
    build_collector_config,
)


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


_ALL_PROVIDER_ENV = {
    "HYPERDX_API_KEY": "hx",  # nosec: B105
    "DASH0_AUTH_TOKEN": "d0",  # nosec: B105
    "DASH0_OTLP_ENDPOINT": "https://ingress.us-west-2.aws.dash0.com",
    "DASH0_DATASET": "prod",
    "LOGFIRE_WRITE_TOKEN": "lf",  # nosec: B105
    # Grafana ships the pre-encoded Basic credential (derived at deploy time),
    # not the raw glc token — see _grafana_basic_auth.
    "GRAFANA_OTLP_AUTH": "MTIzNDU2Omdsy19rZXk=",  # nosec: B105
}

# Raw Grafana deploy-time inputs (never shipped to the container). Kept separate
# from _ALL_PROVIDER_ENV so payload tests can delete them explicitly.
_GRAFANA_RAW_INPUTS = ("GRAFANA_INSTANCE_ID", "GRAFANA_API_KEY")


def test_build_config_fans_out_to_all_providers():
    cfg = build_collector_config(_ALL_PROVIDER_ENV)
    exporters = cfg["exporters"]
    assert set(exporters) == {
        "otlphttp/hyperdx",
        "otlphttp/dash0",
        "otlphttp/logfire",
        "otlphttp/grafana",
    }
    pipelines = cfg["service"]["pipelines"]
    # Both pipelines fan out to every configured provider.
    assert set(pipelines["traces"]["exporters"]) == set(exporters)
    assert set(pipelines["logs"]["exporters"]) == set(exporters)
    for signal in ("traces", "logs"):
        pipeline = pipelines[signal]
        assert pipeline["receivers"] == ["otlp"]
        assert pipeline["processors"] == ["batch"]
    # Receiver is bound to localhost only (no public exposure).
    http = cfg["receivers"]["otlp"]["protocols"]["http"]
    assert http["endpoint"] == "127.0.0.1:4318"


def test_build_config_endpoints_and_headers():
    cfg = build_collector_config(_ALL_PROVIDER_ENV)
    exp = cfg["exporters"]
    # Tokens are referenced via ${env:...}, never inlined.
    assert exp["otlphttp/hyperdx"]["endpoint"] == "https://in-otel.hyperdx.io"
    assert (
        exp["otlphttp/hyperdx"]["headers"]["Authorization"]
        == "Bearer ${env:HYPERDX_API_KEY}"
    )
    assert (
        exp["otlphttp/dash0"]["endpoint"] == "https://ingress.us-west-2.aws.dash0.com"
    )
    assert (
        exp["otlphttp/dash0"]["headers"]["Authorization"]
        == "Bearer ${env:DASH0_AUTH_TOKEN}"
    )
    assert exp["otlphttp/dash0"]["headers"]["Dash0-Dataset"] == "prod"
    assert exp["otlphttp/logfire"]["endpoint"] == "https://logfire-us.pydantic.dev"
    # Grafana uses Basic auth (not Bearer), and the endpoint falls back to the
    # hard-coded regional default when GRAFANA_OTLP_ENDPOINT is unset. The Basic
    # credential is referenced via ${env:...} — never inlined.
    assert exp["otlphttp/grafana"]["endpoint"] == _GRAFANA_DEFAULT_ENDPOINT
    assert (
        exp["otlphttp/grafana"]["headers"]["Authorization"]
        == "Basic ${env:GRAFANA_OTLP_AUTH}"
    )
    # Every exporter has retry + sending queue.
    for block in exp.values():
        assert block["retry_on_failure"]["enabled"] is True
        assert block["sending_queue"]["enabled"] is True


def test_build_config_dash0_dataset_defaults():
    env = {
        "DASH0_AUTH_TOKEN": "d0",  # trunk-ignore(bandit/B105): test fixture
        "DASH0_OTLP_ENDPOINT": "https://ingress.dash0.com",
    }
    cfg = build_collector_config(env)
    assert cfg["exporters"]["otlphttp/dash0"]["headers"]["Dash0-Dataset"] == "default"


def test_build_config_no_providers_omits_logs_pipeline():
    # otelcol rejects a pipeline with an empty exporter list, so with no
    # providers configured the logs pipeline must be omitted entirely.
    pipelines = build_collector_config({})["service"]["pipelines"]
    assert "logs" not in pipelines


def test_build_config_subset_and_empty():
    assert build_collector_config({})["exporters"] == {}
    # Dash0 needs both token and endpoint; token alone is skipped.
    env = {
        "LOGFIRE_WRITE_TOKEN": "lf",  # trunk-ignore(bandit/B105): test fixture
        "DASH0_AUTH_TOKEN": "d0-no-endpoint",  # trunk-ignore(bandit/B105): test fixture
    }
    assert set(build_collector_config(env)["exporters"]) == {"otlphttp/logfire"}


def test_build_config_strips_full_signal_url_to_base():
    env = {
        "HYPERDX_API_KEY": "hx",  # nosec: B105
        "HYPERDX_OTLP_ENDPOINT": "https://in-otel.hyperdx.io/v1/traces",
    }
    cfg = build_collector_config(env)
    # otlphttp appends the signal path itself, so we must hand it the base.
    assert (
        cfg["exporters"]["otlphttp/hyperdx"]["endpoint"] == "https://in-otel.hyperdx.io"
    )


def test_build_config_grafana_endpoint_override_is_stripped_to_base():
    env = {
        "GRAFANA_OTLP_AUTH": "MTIzNDU2Omdsy19rZXk=",  # nosec: B105
        # otlphttp appends /v1/{signal} itself, so a pasted full-signal URL
        # must be stripped back to the base first.
        "GRAFANA_OTLP_ENDPOINT": "https://otlp-gateway-prod-eu-west-2.grafana.net/otlp/v1/traces",
    }
    cfg = build_collector_config(env)
    assert (
        cfg["exporters"]["otlphttp/grafana"]["endpoint"]
        == "https://otlp-gateway-prod-eu-west-2.grafana.net/otlp"
    )


def test_build_config_grafana_bare_host_gets_otlp_path():
    # Grafana Cloud serves OTLP under /otlp; a bare-host override (written like
    # the other providers' root-ingress endpoints) must still resolve to
    # .../otlp/v1/{signal}, not silently drop the /otlp segment.
    env = {
        "GRAFANA_OTLP_AUTH": "MTIzNDU2Omdsy19rZXk=",  # nosec: B105
        "GRAFANA_OTLP_ENDPOINT": "https://otlp-gateway-prod-eu-west-2.grafana.net",
    }
    cfg = build_collector_config(env)
    assert (
        cfg["exporters"]["otlphttp/grafana"]["endpoint"]
        == "https://otlp-gateway-prod-eu-west-2.grafana.net/otlp"
    )


def test_base_endpoint_strips_known_signal_suffixes():
    assert _base_endpoint("https://x.io/v1/traces") == "https://x.io"
    assert _base_endpoint("https://x.io/v1/logs/") == "https://x.io"
    assert _base_endpoint("https://x.io") == "https://x.io"


def test_post_local_targets_localhost_receiver():
    calls: list[dict[str, Any]] = []

    def _post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "headers": headers})

    _post_local("traces", b"OTLP", post=_post)
    assert calls == [
        {
            "url": "http://127.0.0.1:4318/v1/traces",
            "data": b"OTLP",
            "headers": {"Content-Type": "application/x-protobuf"},
        },
    ]


def test_post_local_swallows_errors():
    def _boom(*_a, **_k):
        raise RuntimeError("otelcol not up")

    # Must not raise — otelcol owns reliability once handed off.
    _post_local("logs", b"x", post=_boom)


def test_ensure_otelcol_running_noops_without_providers(monkeypatch):
    """With no provider creds, the collector must NOT start otelcol (an empty
    exporter pipeline is invalid and would boot-loop); it degrades to a no-op.
    Safe to call directly — no providers means no subprocess is launched."""
    monkeypatch.setattr(otel_collector, "_otelcol_proc", None, raising=False)
    for k in _ALL_PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)
    assert _ensure_otelcol_running() is False


class _FakeProc:
    def __init__(self) -> None:
        self.alive = True

    def poll(self) -> int | None:
        return None if self.alive else 1


def test_ensure_otelcol_restarts_crashed_sidecar(monkeypatch):
    """Liveness is by process handle, not a one-shot flag: a crashed sidecar
    (poll() != None) is restarted instead of silently dropping forever."""
    monkeypatch.setattr(otel_collector, "_otelcol_proc", None, raising=False)
    for k, v in _ALL_PROVIDER_ENV.items():
        monkeypatch.setenv(k, v)

    procs: list[_FakeProc] = []

    def _fake_popen(*_a, **_k):
        proc = _FakeProc()
        procs.append(proc)
        return proc

    def _noop_wait(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(otel_collector.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(otel_collector, "_wait_for_port", _noop_wait)

    assert _ensure_otelcol_running() is True
    assert len(procs) == 1  # started once
    assert _ensure_otelcol_running() is True
    assert len(procs) == 1  # still alive -> no restart
    procs[-1].alive = False  # sidecar crashes
    assert _ensure_otelcol_running() is True
    assert len(procs) == 2  # restarted


def test_post_local_logs_non_2xx(monkeypatch, capsys):
    def _post_500(*_a, **_k):
        return _Resp(503)

    _post_local("traces", b"x", post=_post_500)
    err = capsys.readouterr().err
    assert "local_handoff_rejected" in err
    assert "503" in err


def test_collector_secret_payload_reads_env(monkeypatch):
    for k in (*_PROVIDER_SECRET_KEYS, *_GRAFANA_RAW_INPUTS):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DASH0_AUTH_TOKEN", "d0")
    monkeypatch.setenv("DASH0_OTLP_ENDPOINT", "https://ingress.dash0.com")
    monkeypatch.setenv("LOGFIRE_WRITE_TOKEN", "lf")
    assert _collector_secret_payload() == {
        "DASH0_AUTH_TOKEN": "d0",  # trunk-ignore(bandit/B105): test fixture
        "DASH0_OTLP_ENDPOINT": "https://ingress.dash0.com",
        "LOGFIRE_WRITE_TOKEN": "lf",  # trunk-ignore(bandit/B105): test fixture
    }


def test_grafana_basic_auth_encodes_instance_and_token():
    import base64

    cred = _grafana_basic_auth("1718830", "glc_secret")  # nosec: B106
    assert cred is not None
    # Round-trips to the canonical "<instance_id>:<token>" Basic form.
    assert base64.b64decode(cred).decode() == "1718830:glc_secret"
    # Missing either half yields no credential (provider stays disabled).
    assert _grafana_basic_auth("", "glc_secret") is None  # nosec: B106
    assert _grafana_basic_auth("1718830", "") is None


def test_collector_secret_payload_derives_grafana_auth(monkeypatch):
    """The raw glc token + instance id are collapsed to a pre-encoded Basic
    credential at deploy time; the raw inputs never enter the shipped payload."""
    for k in (*_PROVIDER_SECRET_KEYS, *_GRAFANA_RAW_INPUTS):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GRAFANA_INSTANCE_ID", "1718830")
    monkeypatch.setenv("GRAFANA_API_KEY", "glc_secret")  # nosec: B105

    payload = _collector_secret_payload()

    assert payload == {
        "GRAFANA_OTLP_AUTH": _grafana_basic_auth("1718830", "glc_secret"),
    }
    # Raw inputs must not leak through to the collector container.
    assert "GRAFANA_API_KEY" not in payload
    assert "GRAFANA_INSTANCE_ID" not in payload
