from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class GCSObjectRef(BaseModel):
    """Typed handle to a Google Cloud Storage object after a successful write.

    Replaces the bare `str` status returned by `CloudGoogle.to_filesystem` for
    the ETL webhook path so callers can assert on what landed (md5_hash for
    integrity, generation for overwrite detection) and stamp structured logs
    with a verifiable pointer. Bytes stay in the bucket; this object is just
    metadata. Constructed from a `gcsfs.GCSFileSystem.info(path)` response,
    which mirrors the GCS Object resource (camelCase fields from the API).
    """

    bucket: str
    path: str
    gs_uri: str
    generation: int | None = None
    size_bytes: int | None = None
    md5_hash: str | None = None
    etag: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_gcsfs_info(
        cls: type[GCSObjectRef],
        info: dict[str, Any],
        source_path: str,
    ) -> GCSObjectRef:
        """Parse the `dict` returned by `gcsfs.GCSFileSystem.info(path)`.

        gcsfs returns the raw GCS Object resource (camelCase). `source_path`
        is the path the caller wrote to (`gs://bucket/key` or `bucket/key`),
        used as the canonical `gs_uri` and to derive bucket/path when the
        info payload doesn't carry them explicitly.
        """
        gs_uri = (
            source_path if source_path.startswith("gs://") else f"gs://{source_path}"
        )
        without_scheme = gs_uri.removeprefix("gs://")
        bucket, _, path = without_scheme.partition("/")

        # All optional fields parse defensively — this constructor is called
        # AFTER the write has already succeeded, so a malformed GCS readback
        # response must not raise and revert a successful upload to a failure.
        return cls(
            bucket=info.get("bucket") or bucket,
            path=path,
            gs_uri=gs_uri,
            generation=_parse_int(info.get("generation")),
            size_bytes=_parse_int(info.get("size")),
            md5_hash=info.get("md5Hash"),
            etag=info.get("etag"),
            created_at=_parse_iso(info.get("timeCreated")),
        )


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None
