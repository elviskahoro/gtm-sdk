from typing import TYPE_CHECKING, Any

from .bucket_naming import etl_bucket_name, raw_bucket_name
from .destination_type import DestinationType

if TYPE_CHECKING:
    from .filesystem_gcp import CloudGoogle, GCPCredentials


def __getattr__(name: str) -> Any:
    """Load optional GCP helpers only when a caller explicitly requests them."""
    if name in {"CloudGoogle", "GCPCredentials"}:
        from .filesystem_gcp import CloudGoogle, GCPCredentials

        return {"CloudGoogle": CloudGoogle, "GCPCredentials": GCPCredentials}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CloudGoogle",
    "DestinationType",
    "GCPCredentials",
    "etl_bucket_name",
    "raw_bucket_name",
]
