"""Structured logging for webhook handlers.

`libs/telemetry.py` covers OTEL spans (env-gated). This package covers
always-on, machine-readable stdout logs that Modal can capture without any
extra wiring.
"""

from . import structured
from .structured import (
    extract_or_generate_request_id,
    get_request_id,
    get_source,
    log,
    set_request_id,
    set_source,
    webhook_request_context,
)

__all__ = [
    "extract_or_generate_request_id",
    "get_request_id",
    "get_source",
    "log",
    "set_request_id",
    "set_source",
    "structured",
    "webhook_request_context",
]
