from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

import libs.dlt.filesystem_gcp as filesystem_gcp_module
from libs.dlt.filesystem_gcp import CloudGoogle
from libs.filesystem.files import DestinationFileData
from libs.filesystem.refs import GCSObjectRef

if TYPE_CHECKING:
    from collections.abc import Iterator


def _gcs_creds_env() -> dict[str, str]:
    return {
        "GCP_PROJECT_ID": "test-project",
        "GCP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\ntest-key\\n-----END PRIVATE KEY-----",
        "GCP_CLIENT_EMAIL": "test@test-project.iam.gserviceaccount.com",
        "GCP_PRIVATE_KEY_ID": "test-key-id-123",
    }


def _fake_info(path: str, generation: int = 1700000000000001) -> dict[str, object]:
    # Real gcsfs strips the gs:// scheme before returning info — mimic that.
    stripped = path.removeprefix("gs://")
    bucket, _, _ = stripped.partition("/")
    return {
        "bucket": bucket,
        "name": stripped,
        "generation": str(generation),
        "size": 42,
        "md5Hash": "deadbeef==",
        "etag": "etag-test",
        "timeCreated": "2026-05-26T12:00:00Z",
    }


def test_to_filesystem_gcs_with_refs_stamps_metadata_and_returns_refs() -> None:
    """ETL path: metadata flows into fs.open, refs come back populated."""
    with (
        patch.dict(os.environ, _gcs_creds_env()),
        patch("gcsfs.GCSFileSystem") as mock_gcs_fs,
    ):
        mock_fs_instance: MagicMock = MagicMock()
        mock_file: MagicMock = MagicMock()
        mock_fs_instance.open.return_value.__enter__.return_value = mock_file

        def _info_side_effect(path: str) -> dict[str, object]:
            return _fake_info(path)

        mock_fs_instance.info.side_effect = _info_side_effect
        mock_gcs_fs.return_value = mock_fs_instance

        file_data: list[DestinationFileData] = [
            DestinationFileData(
                string="payload-1",
                path="gs://bucket-a/file1.jsonl",
            ),
            DestinationFileData(
                string="payload-2",
                path="gs://bucket-a/file2.jsonl",
            ),
        ]
        metadata = {"git_sha": "abc123", "writer": "export_to_gcp_etl"}

        refs = CloudGoogle.to_filesystem_gcs_with_refs(
            destination_file_data=iter(file_data),
            metadata=metadata,
        )

        assert mock_fs_instance.open.call_count == 2
        for call in mock_fs_instance.open.call_args_list:
            assert call.kwargs["metadata"] == metadata
            assert call.kwargs["mode"] == "w"

        assert len(refs) == 2
        assert refs[0].gs_uri == "gs://bucket-a/file1.jsonl"
        assert refs[0].bucket == "bucket-a"
        assert refs[0].path == "file1.jsonl"
        assert refs[0].generation == 1700000000000001
        assert refs[0].md5_hash == "deadbeef=="
        assert refs[0].size_bytes == 42
        assert refs[1].gs_uri == "gs://bucket-a/file2.jsonl"


def test_to_filesystem_gcs_with_refs_omits_metadata_when_none() -> None:
    """metadata=None must NOT pass a metadata kwarg — keeps the write path
    byte-for-byte identical to the un-stamped (raw) path on the wire."""
    with (
        patch.dict(os.environ, _gcs_creds_env()),
        patch("gcsfs.GCSFileSystem") as mock_gcs_fs,
    ):
        mock_fs_instance: MagicMock = MagicMock()
        mock_file: MagicMock = MagicMock()
        mock_fs_instance.open.return_value.__enter__.return_value = mock_file

        def _info_side_effect(path: str) -> dict[str, object]:
            return _fake_info(path)

        mock_fs_instance.info.side_effect = _info_side_effect
        mock_gcs_fs.return_value = mock_fs_instance

        file_data: list[DestinationFileData] = [
            DestinationFileData(string="x", path="gs://b/x.jsonl"),
        ]
        refs = CloudGoogle.to_filesystem_gcs_with_refs(
            destination_file_data=iter(file_data),
        )

        assert mock_fs_instance.open.call_count == 1
        call = mock_fs_instance.open.call_args
        assert "metadata" not in call.kwargs
        assert call.kwargs["mode"] == "w"
        assert len(refs) == 1


def test_to_filesystem_gcs_with_refs_partial_failure_leaves_partial_refs() -> None:
    """If a mid-batch write fails, the caller-supplied refs_out must already
    hold the refs of objects that DID land — this is the entire point of the
    accumulator parameter; without it the structured-log lineage trail for
    successful writes disappears on partial failure."""
    with (
        patch.dict(os.environ, _gcs_creds_env()),
        patch("gcsfs.GCSFileSystem") as mock_gcs_fs,
    ):
        mock_fs_instance: MagicMock = MagicMock()
        mock_file_ok: MagicMock = MagicMock()
        mock_file_fail: MagicMock = MagicMock()
        mock_file_fail.write.side_effect = OSError("simulated mid-batch failure")

        # First open() returns a working file; second returns one that raises
        # on write. The third entry should never be reached. Force __exit__
        # to NOT suppress exceptions (MagicMock's default magic-method config
        # already does this for context managers, but be explicit so a
        # future stdlib change can't silently invert the test's premise).
        mock_fs_instance.open.return_value.__enter__.side_effect = [
            mock_file_ok,
            mock_file_fail,
        ]
        mock_fs_instance.open.return_value.__exit__.return_value = False

        def _info_side_effect(path: str) -> dict[str, object]:
            return _fake_info(path)

        mock_fs_instance.info.side_effect = _info_side_effect
        mock_gcs_fs.return_value = mock_fs_instance

        file_data: list[DestinationFileData] = [
            DestinationFileData(string="ok", path="gs://b/ok.jsonl"),
            DestinationFileData(string="boom", path="gs://b/boom.jsonl"),
            DestinationFileData(string="never", path="gs://b/never.jsonl"),
        ]
        collected: list[GCSObjectRef] = []

        with pytest.raises(OSError, match="simulated mid-batch failure"):
            CloudGoogle.to_filesystem_gcs_with_refs(
                destination_file_data=iter(file_data),
                refs_out=collected,
            )

        # The first write succeeded and was recorded; the second raised
        # before any ref was appended; the third was never attempted.
        assert len(collected) == 1
        assert collected[0].gs_uri == "gs://b/ok.jsonl"


def test_to_filesystem_gcs_with_refs_info_failure_yields_minimal_ref() -> None:
    """If the write succeeds but the post-write fs.info readback fails (rate
    limit, transient network), the object IS on GCS already and must still
    show up in refs_out — just with the optional fields unpopulated. The
    whole batch must not abort on a best-effort metadata readback."""
    with (
        patch.dict(os.environ, _gcs_creds_env()),
        patch("gcsfs.GCSFileSystem") as mock_gcs_fs,
    ):
        mock_fs_instance: MagicMock = MagicMock()
        mock_file: MagicMock = MagicMock()
        mock_fs_instance.open.return_value.__enter__.return_value = mock_file
        # fs.info raises on every call — simulates a sustained readback outage
        # after the write itself succeeds.
        mock_fs_instance.info.side_effect = OSError("info readback unavailable")
        mock_gcs_fs.return_value = mock_fs_instance

        file_data: list[DestinationFileData] = [
            DestinationFileData(string="ok-1", path="gs://b/one.jsonl"),
            DestinationFileData(string="ok-2", path="gs://b/two.jsonl"),
        ]
        collected: list[GCSObjectRef] = []

        # Must NOT raise — info failure is best-effort.
        refs = CloudGoogle.to_filesystem_gcs_with_refs(
            destination_file_data=iter(file_data),
            refs_out=collected,
        )

        # Both writes recorded; both have only the path-derived fields.
        assert len(refs) == 2
        assert refs is collected
        for ref, path in zip(refs, ["one.jsonl", "two.jsonl"], strict=True):
            assert ref.bucket == "b"
            assert ref.path == path
            assert ref.gs_uri == f"gs://b/{path}"
            assert ref.generation is None
            assert ref.md5_hash is None
            assert ref.size_bytes is None
            assert ref.created_at is None


def test_to_filesystem_with_refs_routes_to_gcs() -> None:
    """The dispatcher delegates GCS URLs to to_filesystem_gcs_with_refs and
    forwards both metadata and refs_out so partial-state semantics survive
    the indirection."""
    with patch.object(
        CloudGoogle,
        "to_filesystem_gcs_with_refs",
        return_value=[],
    ) as mock_gcs_refs:
        file_data: list[DestinationFileData] = [
            DestinationFileData(string="x", path="gs://b/x.jsonl"),
        ]
        sentinel_refs: list[GCSObjectRef] = []
        result = CloudGoogle.to_filesystem_with_refs(
            destination_file_data=iter(file_data),
            bucket_url="gs://b",
            metadata={"k": "v"},
            refs_out=sentinel_refs,
        )

        mock_gcs_refs.assert_called_once()
        kwargs = mock_gcs_refs.call_args.kwargs
        assert kwargs["metadata"] == {"k": "v"}
        assert kwargs["refs_out"] is sentinel_refs
        assert result == []


def test_to_filesystem_with_refs_local_returns_empty() -> None:
    """Local path writes files but returns empty refs (refs are GCS-only)."""
    captured: list[DestinationFileData] = []

    def fake_local(
        *,
        destination_file_data: Iterator[DestinationFileData],
    ) -> None:
        captured.extend(list(destination_file_data))

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.object(filesystem_gcp_module, "to_filesystem_local", fake_local):
            file_data: list[DestinationFileData] = [
                DestinationFileData(string="x", path="x.json"),
            ]
            refs = CloudGoogle.to_filesystem_with_refs(
                destination_file_data=iter(file_data),
                bucket_url=str(Path(tmpdir) / "out"),
            )
            assert refs == []
            assert len(captured) == 1


def test_to_filesystem_with_refs_invalid_url() -> None:
    """None bucket_url raises like the original to_filesystem."""
    file_data: list[DestinationFileData] = [
        DestinationFileData(string="x", path="x.json"),
    ]
    with pytest.raises(ValueError, match="Invalid bucket url: None"):
        CloudGoogle.to_filesystem_with_refs(
            destination_file_data=iter(file_data),
            bucket_url=None,  # type: ignore[arg-type]
        )
