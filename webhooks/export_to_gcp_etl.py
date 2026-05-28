# trunk-ignore-all(ruff/PGH003,trunk/ignore-does-nothing)
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import modal
import orjson
from fastapi import Request
from modal import Image

from libs.dlt.destination_type import DestinationType
from libs.dlt.filesystem_gcp import CloudGoogle
from libs.filesystem.files import DestinationFileData, SourceFileData
from libs.filesystem.refs import GCSObjectRef
from libs.logging.structured import (
    log,
    set_source,
    webhook_request_context,
)
from libs.telemetry import init_log_exporter

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pydantic import BaseModel


# trunk-ignore-begin(ruff/F401,ruff/I001,pyright/reportUnusedImport)
# fmt: off
from src.fathom.webhook.message import (
    Webhook as FathomMessageWebhook,
)
from src.fathom.webhook.call import (
    Webhook as FathomCallWebhook,
)
from src.octolens.webhook import (
    Webhook as OctolensMentionWebhook,
)
from src.rb2b.webhook.visit import (
    Webhook as Rb2bVisitWebhook,
)
from src.caldotcom.webhook.booking import (
    Webhook as CaldotcomBookingWebhook,
)
# fmt: on
# trunk-ignore-end(ruff/F401,ruff/I001,pyright/reportUnusedImport)


if TYPE_CHECKING:
    # Type-check stand-in for the deploy-time placeholder; see
    # webhooks/export_to_attio.py for the full rationale.
    from libs.webhook.protocol import (
        WebhookModelTypeCheckShim as WebhookModelToReplace,
    )


class WebhookModel(WebhookModelToReplace):
    pass


WebhookModel.model_rebuild()

BUCKET_NAME: str = WebhookModel.etl_get_bucket_name()

# Use the bucket name as the `source` so log lines can be filtered by the
# same identifier that names the Modal app.
set_source(BUCKET_NAME)

# Initialize the OTLP log exporter so structured events also ship to whatever
# sink the container's OTEL env vars point at. No-op if none are set.
init_log_exporter(BUCKET_NAME)


def _otel_secret() -> modal.Secret:
    """Inline Modal Secret carrying the OTLP-sink env vars for this container.

    The GCP webhook handlers wire credentials via per-source named Modal
    Secrets (``modal_get_secret_collection_names``), but those collections
    don't carry OTLP routing config. This helper folds the deploy-shell OTEL
    env vars into a separate inline secret so ``libs.telemetry`` can pick
    them up at module import. Missing keys are fine — the exporter is a
    no-op when nothing is wired.
    """
    payload: dict[str, str | None] = {}
    for opt in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
        "HYPERDX_API_KEY",
        "HYPERDX_OTLP_ENDPOINT",
    ):
        v = os.environ.get(opt, "").strip()
        if v:
            payload[opt] = v
    return modal.Secret.from_dict(payload)


image: Image = modal.Image.debian_slim().uv_pip_install(
    "fastapi[standard]",
    "flatsplode",
    "gcsfs",  # https://github.com/fsspec/gcsfs
    "orjson",
    "pyarrow",
    "uuid7",
)
image = image.add_local_python_source(
    *[
        "libs",
        "src",
    ],
)
app = modal.App(
    name=CloudGoogle.clean_bucket_name(
        bucket_name=BUCKET_NAME,
    ),
    image=image,
)

VOLUME: modal.Volume = modal.Volume.from_name(
    BUCKET_NAME,
    create_if_missing=True,
)


@app.function(
    volumes={
        f"/{BUCKET_NAME}": VOLUME,
    },
)
def _get_data_from_storage_remote() -> str:
    path: Path = Path(f"/{BUCKET_NAME}/storage.json")
    if not path.exists():
        error: str = "File not found in the volume"
        raise FileNotFoundError(error)

    return path.read_text()


def _get_storage_source_file_data(
    local_storage_path: str | None,
) -> SourceFileData | None:
    base_model_type: type[BaseModel] | None = WebhookModel.storage_get_base_model_type()
    if base_model_type is None:
        return None

    if local_storage_path is not None:
        return SourceFileData.from_local_storage_path(
            local_storage_path=local_storage_path,
            base_model_type=base_model_type,
        )

    remote_result = _get_data_from_storage_remote.remote()  # trunk-ignore(pyright/reportFunctionMemberAccess,pyrefly/invalid-param-spec,pyrefly/bad-assignment)
    return SourceFileData.from_json_data(
        json_data=remote_result,  # trunk-ignore(pyrefly/bad-argument-type,pyrefly/invalid-param-spec)
        base_model_type=base_model_type,
    )


def _build_provenance_metadata() -> dict[str, str]:
    """Build the GCS custom-object-metadata dict stamped on every ETL write.

    Values must be strings (GCS rejects non-string custom metadata) and are
    read at write time so they reflect the running container, not import
    time. Missing env vars degrade to "unknown" rather than failing the
    write — provenance is best-effort observability, never a hard dep.

    `modal_app` reads `app.name` (the webhook's own Modal app, resolved at
    module import) rather than `os.environ["MODAL_APP"]` — webhook handlers
    deploy as standalone Modal apps named after the bucket (see CLAUDE.md
    `webhooks/` rules), so the env var is not authoritative and reading
    from `app.name` works the same way locally and in deployed containers.
    Do NOT swap in `src.modal_app.MODAL_APP` here: that constant names the
    MAIN app (`src/app.py`), not the per-webhook standalone apps.

    `AI_BUILD_GIT_SHA` and `AI_DEPLOYED_AT` are this repo's canonical
    deploy-time env vars, populated by `Image.env(...)` in `src/app.py`.
    The webhook image construction in this file does NOT yet stamp them,
    so today both fields land as "unknown" — wiring the webhook image to
    set them is a follow-up. Using the canonical names here means the
    rewire just works without revisiting this module.
    """
    return {
        "writer": "export_to_gcp_etl",
        "source_bucket": BUCKET_NAME,
        # `app.name` is typed as `str | None` in modal's stubs even though
        # we explicitly pass `name=...` at construction — coerce to str
        # with a fallback so the metadata stays string-typed for GCS.
        "modal_app": app.name or "unknown",
        "modal_task_id": os.environ.get("MODAL_TASK_ID", "unknown"),
        "git_sha": os.environ.get("AI_BUILD_GIT_SHA", "unknown"),
        "deployed_at": os.environ.get("AI_DEPLOYED_AT", "unknown"),
    }


def _export(webhook: WebhookModel) -> str:
    payload_bytes = len(orjson.dumps(webhook.model_dump()))
    log("webhook.received", payload_bytes=payload_bytes)
    if not webhook.etl_is_valid_webhook():
        reason = webhook.etl_get_invalid_webhook_error_msg()
        log("webhook.validation_failed", reason=reason)
        return reason
    log("webhook.validated", bucket_name=BUCKET_NAME)

    file_data: Iterator[SourceFileData] = iter(
        [
            SourceFileData(
                path=None,
                base_model=webhook,
            ),
        ],
    )
    bucket_url: str = DestinationType.GCS.get_bucket_url_from_bucket_name(
        bucket_name=BUCKET_NAME,
    )
    storage_file_data: SourceFileData | None = _get_storage_source_file_data(
        local_storage_path=None,
    )
    data: Iterator[DestinationFileData] = DestinationFileData.from_source_file_data(
        source_file_data=file_data,
        bucket_url=bucket_url,
        storage=storage_file_data.base_model if storage_file_data else None,
    )
    # `refs_collected` is passed to the writer as `refs_out` so it accumulates
    # refs in-place as each write completes. The `finally` block then emits
    # `webhook.exported` with whatever landed — even on a mid-batch failure,
    # the structured-log lineage trail for the objects that *did* land
    # survives. The webhook path writes one file per invocation today, but
    # the multi-file `local()` entrypoint exercises the same code path.
    #
    # The event is gated on `len(refs_collected) > 0` so a hard failure
    # before the first object lands does NOT emit `webhook.exported` with
    # `count=0` — that would pollute dashboards and make the event
    # indistinguishable from a real empty export. Hard failures are
    # already captured by the `webhook.completed status=error` event
    # emitted in `_handle`.
    refs_collected: list[GCSObjectRef] = []
    try:
        CloudGoogle.to_filesystem_with_refs(
            destination_file_data=data,
            bucket_url=bucket_url,
            metadata=_build_provenance_metadata(),
            refs_out=refs_collected,
        )
    finally:
        if refs_collected:
            log(
                "webhook.exported",
                bucket_name=BUCKET_NAME,
                count=len(refs_collected),
                refs=[ref.model_dump(mode="json") for ref in refs_collected],
            )
    return "Successfully exported to filesystem."


def _handle(webhook: WebhookModel, request: Request) -> str:
    """Webhook request lifecycle. See `webhooks/export_to_attio.py:_handle`."""
    with webhook_request_context(request):
        started = time.perf_counter()
        try:
            body = _export(webhook)
        except Exception as exc:
            log(
                "webhook.completed",
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="error",
                bucket_name=BUCKET_NAME,
                error_type=type(exc).__name__,
                error_msg=str(exc),
            )
            raise
        log(
            "webhook.completed",
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="ok",
            bucket_name=BUCKET_NAME,
        )
        return body


@app.function(
    secrets=[
        modal.Secret.from_name(
            name=name,
        )
        for name in WebhookModel.modal_get_secret_collection_names()
    ]
    + [_otel_secret()],
    region="us-east-1",
    enable_memory_snapshot=False,
)
@modal.fastapi_endpoint(
    method="POST",
    docs=True,
)
@modal.concurrent(
    max_inputs=1000,
)
def web(  # no return annotation: see webhooks/export_to_attio.py for rationale (FastAPI + modal.fastapi_endpoint incompatibility)
    webhook: WebhookModel,
    request: Request,
):
    return _handle(webhook, request)


@app.local_entrypoint()
def local(
    input_folder: str,
    destination_type: str,
    input_path_storage: str | None = None,
) -> None:
    destination_type_enum: DestinationType = DestinationType(destination_type)
    bucket_url: str = destination_type_enum.get_bucket_url_from_bucket_name(
        bucket_name=BUCKET_NAME,
    )
    source_file_data: Iterator[SourceFileData] = SourceFileData.from_input_folder(
        input_folder=input_folder,
        base_model_type=WebhookModel,
        extension=[
            ".json",
        ],
    )
    storage_file_data: SourceFileData | None = _get_storage_source_file_data(
        local_storage_path=input_path_storage,
    )
    destination_file_data: Iterator[DestinationFileData] = (
        DestinationFileData.from_source_file_data(
            source_file_data=source_file_data,
            bucket_url=bucket_url,
            storage=storage_file_data.base_model if storage_file_data else None,
        )
    )
    # See _export: refs_out captures partial state so a mid-batch failure
    # still surfaces the objects that did land. Refs are GCS-specific;
    # the local destination writes files but the writer returns an empty
    # list. GCS output always prints ref summary (including on partial
    # failure, before the exception propagates); local output only prints
    # the success message on an actual success — never gate the success
    # message on the finally block alone, or a raised exception would
    # still print "Successfully exported".
    refs: list[GCSObjectRef] = []
    is_gcs_destination = bucket_url.startswith("gs://")
    success = False
    try:
        CloudGoogle.to_filesystem_with_refs(
            destination_file_data=destination_file_data,
            bucket_url=bucket_url,
            metadata=_build_provenance_metadata(),
            refs_out=refs,
        )
        success = True
    finally:
        if is_gcs_destination:
            if refs:
                print(f"Wrote {len(refs)} object(s):")
                for ref in refs:
                    print(
                        f"  {ref.gs_uri} (generation={ref.generation}, md5={ref.md5_hash})",
                    )
            elif success:
                # Empty input folder: writer succeeded with nothing to write.
                print(f"No objects to write to {bucket_url} (empty input).")
            else:
                print(f"Export to {bucket_url} failed before any object landed.")
        elif success:
            print(f"Successfully exported to local filesystem at {bucket_url}.")
