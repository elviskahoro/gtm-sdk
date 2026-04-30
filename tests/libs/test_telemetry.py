from __future__ import annotations

from libs.telemetry import emit_cli_event, init_tracer


def test_init_tracer_noop_without_env(monkeypatch):
    monkeypatch.delenv("HYPERDX_API_KEY", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracer = init_tracer()
    assert tracer is None


def test_emit_cli_event_noop_without_init():
    # Should not raise even when tracer not initialized
    emit_cli_event("cli.test_event", {"key": "value"})


def test_init_tracer_returns_tracer_with_env(monkeypatch):
    monkeypatch.setenv("HYPERDX_API_KEY", "test-key")
    tracer = init_tracer()
    assert tracer is not None
