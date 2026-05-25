# trunk-ignore-all(ruff/PGH003,trunk/ignore-does-nothing)
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, NamedTuple

import modal
import orjson
from fastapi import Request
from modal import Image
from pydantic import ValidationError
from uuid_extensions import uuid7

from libs.dlt.destination_type import (
    DestinationType,
)
from libs.dlt.filesystem_gcp import CloudGoogle
from libs.filesystem.files import DestinationFileData, FileUtility
from libs.logging.structured import (
    log,
    set_source,
    webhook_request_context,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path


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


class WebhookModel(WebhookModelToReplace):  # type: ignore # trunk-ignore(ruff/F821)
    pass


WebhookModel.model_rebuild()

BUCKET_NAME: str = WebhookModel.raw_get_bucket_name()

# Tag every log line emitted from this container with the bucket name so
# Modal logs filter cleanly per source.
set_source(BUCKET_NAME)

image: Image = modal.Image.debian_slim().uv_pip_install(
    "fastapi[standard]",
    # flatsplode is imported transitively via src/{caldotcom,fathom,rb2b}/utils.py
    # when the WebhookModelToReplace substitution pulls in any of those models.
    "flatsplode",
    "gcsfs",  # https://github.com/fsspec/gcsfs
    "orjson",
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


class SourceFileRaw(NamedTuple):
    file: Path
    content: str

    @staticmethod
    def stream_read_json_as_string(
        path: Path,
    ) -> str:
        with path.open(
            mode="r",
            encoding="utf-8",
        ) as f_in:
            return "\n".join(stripped for line in f_in if (stripped := line.strip()))

    @staticmethod
    def get_data_from_input_folder(
        input_folder: str,
        extension: Iterable[str],
    ) -> Iterator[SourceFileRaw]:
        paths: Iterator[Path] = FileUtility.get_paths(
            input_folder=input_folder,
            extension=extension,
        )
        current_path: Path | None = None
        try:
            path: Path
            for path in paths:
                current_path = path
                yield SourceFileRaw(
                    file=path,
                    content=SourceFileRaw.stream_read_json_as_string(path),
                )

        except ValidationError as e:
            log(
                "webhook.error",
                reason="validation_error",
                path=str(current_path) if current_path is not None else None,
                error=str(e),
            )
            raise

    @staticmethod
    def get_json_data_from_file_data(
        file_data: Iterator[SourceFileRaw],
        bucket_url: str,
    ) -> Iterator[DestinationFileData]:
        for individual_file_data in file_data:
            try:
                yield DestinationFileData(
                    string=individual_file_data.content,
                    path=f"{bucket_url}/{uuid7()!s}.jsonl",
                )

            except (AttributeError, ValueError) as e:
                log(
                    "webhook.error",
                    reason="processing_error",
                    file=str(individual_file_data.file),
                    error=str(e),
                )
                raise


def _handle(json: dict[str, Any], request: Request) -> str:
    """Webhook request lifecycle. See `webhooks/export_to_attio.py:_handle`."""
    with webhook_request_context(request):
        started = time.perf_counter()
        # Measure bytes from the raw orjson output, not from the decoded
        # string — `len(str)` counts code points, so multibyte characters
        # would be silently undercounted in `payload_bytes`.
        json_bytes: bytes = orjson.dumps(json)
        json_data: str = json_bytes.decode(encoding="utf-8")
        log("webhook.received", payload_bytes=len(json_bytes))
        bucket_url: str = DestinationType.GCS.get_bucket_url_from_bucket_name(
            bucket_name=BUCKET_NAME,
        )
        data: Iterator[DestinationFileData] = iter(
            [
                DestinationFileData(
                    string=json_data,
                    path=f"{bucket_url}/{uuid7()!s}.jsonl",
                ),
            ],
        )
        try:
            body = CloudGoogle.to_filesystem(
                destination_file_data=data,
                bucket_url=bucket_url,
            )
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
    ],
    region="us-east4",
    enable_memory_snapshot=False,
)
@modal.concurrent(
    max_inputs=1000,
)
@modal.fastapi_endpoint(
    method="POST",
    docs=True,
)
def web(
    json: dict[str, Any],
    request: Request,
) -> str:
    return _handle(json, request)


@app.local_entrypoint()
def local(
    input_folder: str,
    destination_type: str,
    bucket_name: str = BUCKET_NAME,
) -> None:
    destination_type_enum: DestinationType = DestinationType.from_string(
        destination_type,
    )
    bucket_url: str = destination_type_enum.get_bucket_url_from_bucket_name(
        bucket_name=bucket_name,
    )

    file_data: Iterator[SourceFileRaw] = SourceFileRaw.get_data_from_input_folder(
        input_folder=input_folder,
        extension=[
            ".json",
            ".jsonl",
        ],
    )
    data: Iterator[DestinationFileData] = SourceFileRaw.get_json_data_from_file_data(
        file_data=file_data,
        bucket_url=bucket_url,
    )
    response: str = CloudGoogle.to_filesystem(
        destination_file_data=data,
        bucket_url=bucket_url,
    )
    print(response)
