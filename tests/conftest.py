from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from attio.errors import SDKError  # noqa: E402

from libs.attio.sdk_boundary import get_attio_sdk_client_class  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _neuter_telemetry_network() -> Iterator[None]:
    """Stop telemetry unit tests from phoning home at interpreter shutdown.

    The telemetry tests (``tests/test_smoke_telemetry.py``,
    ``tests/libs/test_telemetry.py``) call ``init_tracer`` / ``init_log_exporter``
    with real-ish env (``HYPERDX_API_KEY="test-key"`` etc.), which builds a live
    ``OTLPSpanExporter``/``OTLPLogExporter`` behind a ``BatchSpanProcessor`` and
    registers ``atexit.register(provider.shutdown)`` (see ``libs/telemetry.py``).
    The global ``TracerProvider`` is process-wide and set-once, so any later
    ``emit_cli_event`` span lands in whichever exporter was installed first and
    gets flushed to ``in-otel.hyperdx.io`` at process exit — using a bogus key,
    so the server returns 404 and the OTEL SDK prints
    ``Failed to export span batch code: 404, reason: Not Found`` *after* the
    pytest summary. It looks like a failure on a passing run (the Dagger CI job
    is green regardless — only pytest's exit code reddens it).

    Neuter the network boundary for the whole session so nothing is ever sent,
    regardless of test ordering or which provider wins the set-once global:
    subclass the real OTLP HTTP exporters and no-op their ``export``/
    ``shutdown``/``force_flush`` (construction kwargs like ``endpoint=`` are
    still accepted, so introspection tests that nest their own
    ``patch(...OTLPLogExporter, autospec=True)`` are unaffected).

    Lazy ``from ... import OTLPSpanExporter`` inside ``libs/telemetry.py`` reads
    the source-module attribute at call time, so patching it there takes effect.
    The collector path (``TELEMETRY_COLLECTOR_APP`` unset, the CI default) is
    left alone: its ``_spawn_collector`` Modal RPC already swallows its own
    errors and never prints the OTLP HTTP 404, and it is exercised directly by
    the spawn-exporter unit tests.
    """
    from unittest.mock import patch

    from opentelemetry.exporter.otlp.proto.http._log_exporter import (
        OTLPLogExporter as _RealLogExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as _RealSpanExporter,
    )
    from opentelemetry.sdk._logs.export import LogRecordExportResult
    from opentelemetry.sdk.trace.export import SpanExportResult

    class _NoNetworkSpanExporter(_RealSpanExporter):  # type: ignore[misc]
        def export(self, spans: Any) -> Any:  # noqa: ARG002
            return SpanExportResult.SUCCESS

        def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
            return True

        def shutdown(self) -> None:
            return None

    class _NoNetworkLogExporter(_RealLogExporter):  # type: ignore[misc]
        def export(self, batch: Any) -> Any:  # noqa: ARG002
            return LogRecordExportResult.SUCCESS

        def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
            return True

        def shutdown(self) -> None:
            return None

    patchers = [
        patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
            _NoNetworkSpanExporter,
        ),
        patch(
            "opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter",
            _NoNetworkLogExporter,
        ),
    ]
    for p in patchers:
        p.start()
    try:
        yield
    finally:
        for p in patchers:
            p.stop()


# Attio API keys are 64 chars per the API's own validation
# ("API Keys should be 64 characters long" — surfaced in the 401 body).
# Skip rather than 401 when ATTIO_API_KEY is set to a shorter stub value, so
# a dev shell that exports a placeholder doesn't masquerade as an auth failure.
_ATTIO_KEY_MIN_LEN = 64


@pytest.fixture(scope="session")
def attio_api_key() -> str:
    key = os.environ.get("ATTIO_API_KEY", "").strip()
    if not key:
        pytest.skip(
            "Attio integration tests gated on ATTIO_API_KEY",
        )
    if len(key) < _ATTIO_KEY_MIN_LEN:
        pytest.skip(
            f"ATTIO_API_KEY looks like a stub ({len(key)} chars; "
            f"expected >= {_ATTIO_KEY_MIN_LEN})",
        )
    return key


@pytest.fixture(scope="session")
def modal_credentials_available() -> bool:
    token_id = os.environ.get("MODAL_TOKEN_ID", "").strip()
    token_secret = os.environ.get("MODAL_TOKEN_SECRET", "").strip()
    return bool(token_id and token_secret)


@pytest.fixture(scope="session")
def attio_auth_probe(attio_api_key: str) -> None:
    # Cheap auth probe so a stale/invalid ATTIO_API_KEY skips integration tests
    # rather than 401-ing through every one. Runs once per session. Only auth
    # failures (401/403) get converted to skip — every other error propagates
    # so genuine SDK / schema / network regressions still surface.
    sdk_client_class = get_attio_sdk_client_class()
    probe_client = sdk_client_class(oauth2=attio_api_key)
    try:
        probe_client.records.post_v2_objects_object_records_query(
            object="people",
            filter_={},
            limit=1,
        )
    except SDKError as exc:
        if exc.status_code in (401, 403):
            pytest.skip(
                f"Attio credentials present but auth probe returned {exc.status_code}: {exc}",
            )
        raise


@pytest.fixture
def client(
    attio_api_key: str,
    attio_auth_probe: None,
) -> Any:
    sdk_client_class = get_attio_sdk_client_class()
    return sdk_client_class(oauth2=attio_api_key)


@pytest.fixture(scope="session")  # pyright: ignore[reportUntypedFunctionDecorator]
def social_mention_bootstrapped(
    attio_api_key: str,
    attio_auth_probe: None,  # noqa: ARG001 — chains auth probe
) -> None:
    # social_mention is a custom Attio object that must be bootstrapped via
    # scripts/attio-social_mentions-bootstrap.py --apply before any mention upsert
    # works. If a workspace was created without running bootstrap (e.g. a
    # fresh dev workspace), skip mention-writer integration tests with a
    # clear pointer rather than erroring deep inside _ensure_select_options.
    sdk_client_class = get_attio_sdk_client_class()
    probe_client = sdk_client_class(oauth2=attio_api_key)
    try:
        probe_client.records.post_v2_objects_object_records_query(
            object="social_mention",
            filter_={},
            limit=1,
        )
    except SDKError as exc:
        if exc.status_code == 404:
            pytest.skip(
                "social_mention object not bootstrapped in this Attio workspace; "
                "run `scripts/attio-social_mentions-bootstrap.py --apply` against the "
                "target workspace before running this test.",
            )
        raise


@pytest.fixture
def created_people_record_ids() -> list[str]:
    return []


@pytest.fixture
def cleanup_people_records(
    client: Any,
    created_people_record_ids: list[str],
) -> Iterator[None]:
    yield
    for record_id in created_people_record_ids:
        try:
            client.records.delete_v2_objects_object_records_record_id_(
                object="people",
                record_id=record_id,
            )

        except Exception as exc:
            print(
                f"Warning: failed to delete Attio test person record {record_id}: {exc}",
                file=sys.stderr,
            )


@pytest.fixture
def created_mention_record_ids() -> list[str]:
    return []


@pytest.fixture
def cleanup_mention_records(
    client: Any,
    created_mention_record_ids: list[str],
) -> Iterator[None]:
    yield
    for record_id in created_mention_record_ids:
        try:
            client.records.delete_v2_objects_object_records_record_id_(
                object="social_mention",
                record_id=record_id,
            )

        except Exception as exc:
            print(
                f"Warning: failed to delete Attio test social_mention record {record_id}: {exc}",
                file=sys.stderr,
            )
