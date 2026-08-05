from importlib import import_module
from typing import TYPE_CHECKING

from .bucket_naming import etl_bucket_name, raw_bucket_name
from .destination_type import DestinationType

if TYPE_CHECKING:
    from .filesystem_gcp import CloudGoogle, GCPCredentials

__all__ = [
    "CloudGoogle",
    "DestinationType",
    "GCPCredentials",
    "etl_bucket_name",
    "raw_bucket_name",
]


def __getattr__(name: str) -> object:
    """Resolve optional GCP helpers only when explicitly requested."""
    if name in {"CloudGoogle", "GCPCredentials"}:
        module = import_module(".filesystem_gcp", __name__)
        return getattr(module, name)
    error_message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(error_message)
