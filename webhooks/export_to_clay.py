# trunk-ignore-all(ruff/PGH003,trunk/ignore-does-nothing)
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import modal
import orjson
from fastapi import Request
from modal import Image

from libs import infisical
from libs.logging.structured import log, set_source, webhook_request_context
from libs.telemetry import init_log_exporter, init_tracer, span
from src.clay.export import execute
from src.secrets_bootstrap import bootstrap_secret

# trunk-ignore-begin(ruff/F401,ruff/I001,pyright/reportUnusedImport)
# fmt: off
from src.octolens.webhook.mention import (
    Webhook as OctolensMentionWebhook,
)
from src.rb2b.webhook.visit import (
    Webhook as Rb2bVisitWebhook,
)

# fmt: on
# trunk-ignore-end(ruff/F401,ruff/I001,pyright/reportUnusedImport)

if TYPE_CHECKING:
    from libs.webhook.protocol import (
        WebhookModelTypeCheckShim as WebhookModelToReplace,
    )


class WebhookModel(WebhookModelToReplace):
    pass


WebhookModel.model_rebuild()

APP_NAME: str = WebhookModel.clay_get_app_name()
set_source(APP_NAME)
init_log_exporter(APP_NAME)
init_tracer(APP_NAME)

image: Image = modal.Image.debian_slim().uv_pip_install(
    "fastapi[standard]",
    "infisicalsdk>=1.0.16",
    "opentelemetry-api",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-sdk",
    "opentelemetry-semantic-conventions",
    "orjson",
    "requests>=2.33.1",
    "uuid7",
)
image = image.add_local_python_source("libs", "src")
app = modal.App(name=APP_NAME, image=image)


def _export(webhook: WebhookModel) -> str:
    payload_bytes = len(orjson.dumps(webhook.model_dump()))
    log("webhook.received", payload_bytes=payload_bytes)
    if not webhook.clay_is_valid_webhook():
        reason = webhook.clay_get_invalid_webhook_error_msg()
        log("webhook.validation_failed", reason=reason)
        return reason

    row = webhook.clay_get_row()
    url_key = WebhookModel.clay_get_webhook_url_secret_name()
    token_key = WebhookModel.clay_get_webhook_auth_token_secret_name()
    with infisical.fetch_all([url_key, token_key]) as secrets:
        log("webhook.validated", row_count=1, event_id=row["event_id"])
        result = execute(
            webhook_url=secrets[url_key],
            auth_token=secrets[token_key],
            row=row,
        )
    log("clay.row_posted", event_id=row["event_id"], status_code=result.status_code)
    return result.body()


def _handle(webhook: WebhookModel, request: Request) -> str:
    """Webhook request lifecycle, kept separately for direct unit testing."""
    with webhook_request_context(request) as request_id:
        started = time.perf_counter()
        with span("webhook", source=APP_NAME, request_id=request_id):
            try:
                body = _export(webhook)
            except Exception as exc:
                log(
                    "webhook.completed",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    status="error",
                    error_type=type(exc).__name__,
                    error_msg=str(exc),
                )
                raise
            log(
                "webhook.completed",
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="ok",
            )
            return body


@app.function(
    secrets=[bootstrap_secret()],
    region="us-east-1",
    enable_memory_snapshot=False,
)
@modal.fastapi_endpoint(method="POST", docs=True)
@modal.concurrent(max_inputs=1000)
def web(
    webhook: WebhookModel,
    request: Request,
):  # no return annotation: see export_to_attio.py for FastAPI/Modal rationale
    return _handle(webhook, request)


@app.local_entrypoint()
def local(input_file: str) -> None:
    payload = orjson.loads(Path(input_file).read_bytes())
    webhook = WebhookModel.model_validate(payload)
    print(_export(webhook))
