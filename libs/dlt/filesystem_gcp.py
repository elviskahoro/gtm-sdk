from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import gcsfs

from libs.dlt.filesystem_local import to_filesystem_local
from libs.filesystem.files import DestinationFileData
from libs.filesystem.refs import GCSObjectRef

if TYPE_CHECKING:
    from collections.abc import Iterator


class GCPCredentials(NamedTuple):
    project_id: str | None
    private_key: str | None
    client_email: str | None
    private_key_id: str | None

    @classmethod
    def get_env_vars(cls: type[GCPCredentials]) -> GCPCredentials:
        """Get GCP credentials from environment variables.

        Returns:
            GCPCredentials containing project_id, private_key, client_email, and private_key_id
        """
        gcp_client_email = os.environ.get(
            "GCP_CLIENT_EMAIL",
            None,
        )
        gcp_project_id = os.environ.get(
            "GCP_PROJECT_ID",
            None,
        )
        gcp_private_key = os.environ.get(
            "GCP_PRIVATE_KEY",
            None,
        )
        gcp_private_key_id = os.environ.get(
            "GCP_PRIVATE_KEY_ID",
            None,
        )
        if gcp_private_key:
            # Handle escaped newlines in the private key
            # Strip quotes first
            gcp_private_key = gcp_private_key.strip("\"'")
            # Replace literal \n (backslash + n) with actual newlines
            gcp_private_key = gcp_private_key.replace("\\n", "\n")
            # Also remove any remaining backslashes before newlines (from double-escaping)
            gcp_private_key = gcp_private_key.replace("\\\n", "\n")

        return cls(
            project_id=gcp_project_id,
            private_key=gcp_private_key,
            client_email=gcp_client_email,
            private_key_id=gcp_private_key_id,
        )

    @classmethod
    def from_env_required(cls: type[GCPCredentials]) -> GCPCredentials:
        """Get GCP credentials from environment variables (all required).

        Returns:
            GCPCredentials with all fields populated from environment

        Raises:
            ValueError: If any required environment variable is missing
        """
        credentials = cls.get_env_vars()
        if (
            credentials.project_id is None
            or credentials.private_key is None
            or credentials.client_email is None
            or credentials.private_key_id is None
        ):
            error_msg = (
                "All GCP credentials must be set via environment variables: "
                "GCP_PROJECT_ID, GCP_PRIVATE_KEY, GCP_CLIENT_EMAIL, and GCP_PRIVATE_KEY_ID"
            )
            raise ValueError(error_msg)
        return credentials

    def to_service_account_token(self) -> dict[str, str]:
        """Convert credentials to a service account token dictionary for gcsfs.

        Returns:
            Token dictionary suitable for gcsfs.GCSFileSystem

        Raises:
            ValueError: If any credential field is None
        """
        if (
            self.project_id is None
            or self.private_key is None
            or self.client_email is None
            or self.private_key_id is None
        ):
            error_msg = "Cannot create token from incomplete credentials"
            raise ValueError(error_msg)

        return {
            "type": "service_account",
            "project_id": self.project_id,
            "private_key_id": self.private_key_id,
            "private_key": self.private_key,
            "client_email": self.client_email,
            "token_uri": "https://oauth2.googleapis.com/token",  # nosec B105
        }


class CloudGoogle:
    """Helper class for Google Cloud Platform operations."""

    @staticmethod
    def clean_bucket_name(bucket_name: str) -> str:
        """Clean bucket name by replacing hyphens with underscores.

        Args:
            bucket_name: The bucket name to clean

        Returns:
            The cleaned bucket name
        """
        return bucket_name.replace(
            "-",
            "_",
        )

    @staticmethod
    def bucket_url_from_bucket_name(bucket_name: str) -> str:
        """Generate a GCS bucket URL from bucket name.

        Args:
            bucket_name: The bucket name

        Returns:
            The GCS bucket URL
        """
        return f"gs://{bucket_name}"

    @staticmethod
    def strip_bucket_url(bucket_url: str) -> str:
        """Strip GCS prefix from bucket URL and clean the name.

        Args:
            bucket_url: The GCS bucket URL

        Returns:
            The cleaned bucket name
        """
        return bucket_url.replace(
            "gs://",
            "",
        ).replace(
            "-",
            "_",
        )

    @staticmethod
    def to_filesystem(
        destination_file_data: Iterator[DestinationFileData],
        bucket_url: str | None,
    ) -> str:
        """Export data to filesystem (GCS or local).

        Args:
            destination_file_data: Iterator of file data to export
            bucket_url: The target bucket URL or local path

        Returns:
            Success message

        Raises:
            ValueError: If bucket_url is invalid
        """
        match bucket_url:
            case str() as url if url.startswith("gs://"):
                CloudGoogle.to_filesystem_gcs(
                    destination_file_data=destination_file_data,
                )

            case str():
                bucket_url_path: Path = Path(bucket_url)
                bucket_url_path.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                to_filesystem_local(
                    destination_file_data=destination_file_data,
                )

            case _:
                error_msg: str = f"Invalid bucket url: {bucket_url}"
                raise ValueError(error_msg)

        return "Successfully exported to filesystem."

    @staticmethod
    def to_filesystem_gcs(
        destination_file_data: Iterator[DestinationFileData],
    ) -> None:
        """Export data specifically to Google Cloud Storage.

        IMPORTANT: This method ONLY uses credentials from environment variables.
        No other authentication method is supported.

        Required environment variables:
        - GCP_PROJECT_ID: GCP project ID
        - GCP_PRIVATE_KEY: Service account private key
        - GCP_CLIENT_EMAIL: Service account email
        - GCP_PRIVATE_KEY_ID: Service account private key ID

        Args:
            destination_file_data: Iterator of file data to export

        Raises:
            ValueError: If any required GCP environment variable is not set
        """
        # Force use of environment variables only - will raise if any are missing
        credentials: GCPCredentials = GCPCredentials.from_env_required()

        # Create GCS filesystem with service account token from env vars
        fs: gcsfs.GCSFileSystem = gcsfs.GCSFileSystem(
            project=credentials.project_id,
            token=credentials.to_service_account_token(),
        )

        for file_data in destination_file_data:
            with fs.open(
                path=file_data.path,
                mode="w",
            ) as f:
                f.write(
                    file_data.string,
                )

    @staticmethod
    def to_filesystem_gcs_with_refs(
        destination_file_data: Iterator[DestinationFileData],
        metadata: dict[str, str] | None = None,
        refs_out: list[GCSObjectRef] | None = None,
    ) -> list[GCSObjectRef]:
        """Write to GCS like `to_filesystem_gcs`, but stamp provenance metadata
        on each object and return typed `GCSObjectRef` handles for the writes.

        Used by the ETL webhook path so structured logs and tests have a
        verifiable pointer (md5_hash, generation) to what landed. The raw
        webhook path keeps using `to_filesystem_gcs` — no behavior change there.

        `metadata` is forwarded to gcsfs as GCS custom object metadata; pass
        only string-typed values (GCS rejects non-strings) and keep individual
        values under ~1 KiB. When `metadata` is None, the kwarg is omitted
        entirely to match the un-stamped write path byte-for-byte.

        `refs_out`, when provided, is the SAME list that gets returned — refs
        are appended as each write completes, so a caller wrapping this in
        try/finally can read partial-success state after a mid-batch failure
        and still emit lineage for the objects that landed. Bytes already on
        GCS carry their custom metadata stamps independently, but `refs_out`
        is the only path to the structured-log echo on partial failure.

        After each write, `fs.info(path)` is called to read back the GCS
        Object resource (generation, md5Hash, etag, size, timeCreated) — one
        extra round trip per file vs. `to_filesystem_gcs`.
        """
        credentials: GCPCredentials = GCPCredentials.from_env_required()
        fs: gcsfs.GCSFileSystem = gcsfs.GCSFileSystem(
            project=credentials.project_id,
            token=credentials.to_service_account_token(),
        )

        open_kwargs: dict[str, Any] = {"mode": "w"}
        if metadata is not None:
            open_kwargs["metadata"] = metadata

        refs: list[GCSObjectRef] = refs_out if refs_out is not None else []
        for file_data in destination_file_data:
            with fs.open(
                path=file_data.path,
                **open_kwargs,
            ) as f:
                f.write(
                    file_data.string,
                )

            # `fs.info` is a SEPARATE GCS API call after the upload. It can
            # fail (rate limit, transient network) even when the write
            # succeeded — the bytes are already on GCS with their custom
            # metadata stamp. Treat the readback as best-effort: on failure,
            # record a minimal ref (gs_uri / bucket / path only) so the
            # caller still sees that this object landed.
            try:
                info: dict[str, Any] = fs.info(file_data.path)
            except Exception:  # noqa: BLE001 — best-effort readback by design
                info = {}
            refs.append(
                GCSObjectRef.from_gcsfs_info(
                    info=info,
                    source_path=file_data.path,
                ),
            )

        return refs

    @staticmethod
    def to_filesystem_with_refs(
        destination_file_data: Iterator[DestinationFileData],
        bucket_url: str | None,
        metadata: dict[str, str] | None = None,
        refs_out: list[GCSObjectRef] | None = None,
    ) -> list[GCSObjectRef]:
        """Ref-returning sibling of `to_filesystem`. GCS path stamps `metadata`
        and returns `GCSObjectRef`s; local path writes files (for the
        `local()` dev entrypoint) and returns an empty list — refs are
        GCS-specific.

        `refs_out`, when provided, is mutated in-place as each GCS write
        completes — callers wrapping this in try/finally see partial state
        after a mid-batch failure. See `to_filesystem_gcs_with_refs`.

        Used only by `webhooks/export_to_gcp_etl.py`. The raw webhook handler
        keeps calling `to_filesystem`, which intentionally has no metadata
        stamping and no extra `fs.info` round trip.
        """
        match bucket_url:
            case str() as url if url.startswith("gs://"):
                return CloudGoogle.to_filesystem_gcs_with_refs(
                    destination_file_data=destination_file_data,
                    metadata=metadata,
                    refs_out=refs_out,
                )

            case str():
                bucket_url_path: Path = Path(bucket_url)
                bucket_url_path.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                to_filesystem_local(
                    destination_file_data=destination_file_data,
                )
                return refs_out if refs_out is not None else []

            case _:
                error_msg: str = f"Invalid bucket url: {bucket_url}"
                raise ValueError(error_msg)

    @staticmethod
    def export_to_filesystem(
        destination_file_data: Iterator[DestinationFileData],
        bucket_url: str,
    ) -> str:
        """Export data to filesystem (GCS or local).

        Args:
            destination_file_data: Iterator of file data to export
            bucket_url: The target bucket URL or local path

        Returns:
            Success message

        Raises:
            ValueError: If bucket_url is invalid
        """
        return CloudGoogle.to_filesystem(destination_file_data, bucket_url)

    @staticmethod
    def export_to_gcs(
        destination_file_data: Iterator[DestinationFileData],
    ) -> None:
        """Export data specifically to Google Cloud Storage.

        Args:
            destination_file_data: Iterator of file data to export

        Raises:
            ValueError: If GCP credentials are not properly set
        """
        CloudGoogle.to_filesystem_gcs(destination_file_data)


def test_clean_bucket_name() -> None:
    """Test clean_bucket_name replaces hyphens with underscores."""
    assert CloudGoogle.clean_bucket_name("my-bucket-name") == "my_bucket_name"
    assert CloudGoogle.clean_bucket_name("my_bucket_name") == "my_bucket_name"
    assert CloudGoogle.clean_bucket_name("my-bucket-name-123") == "my_bucket_name_123"
    assert CloudGoogle.clean_bucket_name("") == ""


def test_bucket_url_from_bucket_name() -> None:
    """Test bucket_url_from_bucket_name generates correct GCS URLs."""
    assert CloudGoogle.bucket_url_from_bucket_name("my-bucket") == "gs://my-bucket"
    assert CloudGoogle.bucket_url_from_bucket_name("test_bucket") == "gs://test_bucket"
    assert CloudGoogle.bucket_url_from_bucket_name("") == "gs://"


def test_strip_bucket_url() -> None:
    """Test strip_bucket_url removes gs:// prefix and cleans bucket name."""
    assert CloudGoogle.strip_bucket_url("gs://my-bucket-name") == "my_bucket_name"
    assert CloudGoogle.strip_bucket_url("gs://my_bucket_name") == "my_bucket_name"
    assert CloudGoogle.strip_bucket_url("my-bucket-name") == "my_bucket_name"
    assert CloudGoogle.strip_bucket_url("") == ""


def test_get_env_vars_with_all_vars() -> None:
    """Test _get_env_vars returns credentials when all env vars are set."""
    from unittest.mock import patch

    with patch.dict(
        os.environ,
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\ntest-key\\n-----END PRIVATE KEY-----",
            "GCP_CLIENT_EMAIL": "test@test-project.iam.gserviceaccount.com",
            "GCP_PRIVATE_KEY_ID": "test-key-id-123",
        },
    ):
        creds: GCPCredentials = GCPCredentials.get_env_vars()
        assert creds.project_id == "test-project"
        assert (
            creds.private_key
            == "-----BEGIN PRIVATE KEY-----\ntest-key\n-----END PRIVATE KEY-----"
        )
        assert creds.client_email == "test@test-project.iam.gserviceaccount.com"
        assert creds.private_key_id == "test-key-id-123"


def test_get_env_vars_with_no_vars() -> None:
    """Test get_env_vars returns None values when env vars are not set."""
    from unittest.mock import patch

    with patch.dict(os.environ, {}, clear=True):
        creds: GCPCredentials = GCPCredentials.get_env_vars()
        assert creds.project_id is None
        assert creds.private_key is None
        assert creds.client_email is None
        assert creds.private_key_id is None


def test_get_env_vars_with_partial_vars() -> None:
    """Test get_env_vars with only some env vars set."""
    from unittest.mock import patch

    with patch.dict(
        os.environ,
        {"GCP_PROJECT_ID": "test-project"},
    ):
        creds: GCPCredentials = GCPCredentials.get_env_vars()
        assert creds.project_id == "test-project"
        assert creds.private_key is None
        assert creds.client_email is None
        assert creds.private_key_id is None


def test_get_env_vars_public_method() -> None:
    """Test that get_env_vars works as a class method."""
    from unittest.mock import patch

    test_credentials: GCPCredentials = GCPCredentials(
        project_id="test-project",
        private_key="-----BEGIN PRIVATE KEY-----\ntest-key\n-----END PRIVATE KEY-----",
        client_email="test@test-project.iam.gserviceaccount.com",
        private_key_id="test-key-id-123",
    )

    with patch.dict(
        os.environ,
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\ntest-key\\n-----END PRIVATE KEY-----",
            "GCP_CLIENT_EMAIL": "test@test-project.iam.gserviceaccount.com",
            "GCP_PRIVATE_KEY_ID": "test-key-id-123",
        },
    ):
        result: GCPCredentials = GCPCredentials.get_env_vars()
        assert result == test_credentials


def test_to_filesystem_with_gcs_url() -> None:
    """Test to_filesystem routes to GCS when URL starts with gs://."""
    from unittest.mock import patch

    with patch.object(CloudGoogle, "to_filesystem_gcs") as mock_to_gcs:
        # Mock to_filesystem_gcs to avoid credential check
        mock_to_gcs.return_value = None

        file_data: list[DestinationFileData] = [
            DestinationFileData(string="test", path="test.json"),
        ]
        result: str = CloudGoogle.to_filesystem(iter(file_data), "gs://my-bucket")

        mock_to_gcs.assert_called_once()
        assert result == "Successfully exported to filesystem."


def test_to_filesystem_with_local_path() -> None:
    """Test to_filesystem routes to local filesystem for non-GCS paths."""
    import tempfile

    # Create a list to hold the data passed to to_filesystem_local
    captured_data: list[DestinationFileData] = []

    def mock_to_filesystem_local(
        *,
        destination_file_data: Iterator[DestinationFileData],
    ) -> None:
        # Consume the iterator to capture the data
        captured_data.extend(list(destination_file_data))

    # Store the original function
    original_func = globals()["to_filesystem_local"]

    with tempfile.TemporaryDirectory() as tmpdir:
        # Replace the function in globals
        globals()["to_filesystem_local"] = mock_to_filesystem_local

        try:
            # Use temp directory path
            local_path: Path = Path(tmpdir) / "local" / "path"

            file_data: list[DestinationFileData] = [
                DestinationFileData(string="test", path="test.json"),
            ]
            result: str = CloudGoogle.to_filesystem(iter(file_data), str(local_path))

            # Verify the directory was created
            assert local_path.exists()
            assert local_path.is_dir()

            # Verify to_filesystem_local was called
            assert len(captured_data) == 1
            assert captured_data[0].string == "test"
            assert result == "Successfully exported to filesystem."

        finally:
            # Restore the original function
            globals()["to_filesystem_local"] = original_func


def test_to_filesystem_with_invalid_url() -> None:
    """Test to_filesystem raises ValueError for None bucket_url."""
    import pytest

    file_data: list[DestinationFileData] = [
        DestinationFileData(string="test", path="test.json"),
    ]
    with pytest.raises(ValueError, match="Invalid bucket url: None"):
        CloudGoogle.to_filesystem(iter(file_data), None)  # type: ignore[arg-type]


def test_to_filesystem_gcs_success() -> None:
    """Test to_filesystem_gcs successfully writes files to GCS."""
    from unittest.mock import MagicMock, patch

    with (
        patch.dict(
            os.environ,
            {
                "GCP_PROJECT_ID": "test-project",
                "GCP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\ntest-key\\n-----END PRIVATE KEY-----",
                "GCP_CLIENT_EMAIL": "test@test-project.iam.gserviceaccount.com",
                "GCP_PRIVATE_KEY_ID": "test-key-id-123",
            },
        ),
        patch("gcsfs.GCSFileSystem") as mock_gcs_fs,
    ):
        # Setup mock filesystem
        mock_fs_instance: MagicMock = MagicMock()
        mock_file: MagicMock = MagicMock()
        mock_fs_instance.open.return_value.__enter__.return_value = mock_file
        mock_gcs_fs.return_value = mock_fs_instance

        # Create test data
        file_data: list[DestinationFileData] = [
            DestinationFileData(
                string="test content 1",
                path="gs://bucket/file1.json",
            ),
            DestinationFileData(
                string="test content 2",
                path="gs://bucket/file2.json",
            ),
        ]

        # Execute
        CloudGoogle.to_filesystem_gcs(iter(file_data))

        # Verify
        assert mock_gcs_fs.call_count == 1
        assert mock_fs_instance.open.call_count == 2
        assert mock_file.write.call_count == 2
        mock_file.write.assert_any_call("test content 1")
        mock_file.write.assert_any_call("test content 2")


def test_to_filesystem_gcs_missing_credentials() -> None:
    """Test to_filesystem_gcs raises ValueError when credentials are missing."""
    from unittest.mock import patch

    import pytest

    file_data: list[DestinationFileData] = [
        DestinationFileData(string="test", path="test.json"),
    ]

    with (
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(
            ValueError,
            match="All GCP credentials must be set via environment variables",
        ),
    ):
        CloudGoogle.to_filesystem_gcs(iter(file_data))


def test_to_filesystem_gcs_partial_credentials() -> None:
    """Test to_filesystem_gcs raises ValueError when some credentials are missing."""
    from unittest.mock import patch

    import pytest

    file_data: list[DestinationFileData] = [
        DestinationFileData(string="test", path="test.json"),
    ]

    with (
        patch.dict(
            os.environ,
            {
                "GCP_PROJECT_ID": "test-project",
                "GCP_CLIENT_EMAIL": "test@test-project.iam.gserviceaccount.com",
            },
        ),
        pytest.raises(
            ValueError,
            match="All GCP credentials must be set via environment variables",
        ),
    ):
        CloudGoogle.to_filesystem_gcs(iter(file_data))


def test_to_filesystem_gcs_empty_iterator() -> None:
    """Test to_filesystem_gcs handles empty iterator gracefully."""
    from unittest.mock import MagicMock, patch

    with (
        patch.dict(
            os.environ,
            {
                "GCP_PROJECT_ID": "test-project",
                "GCP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\ntest-key\\n-----END PRIVATE KEY-----",
                "GCP_CLIENT_EMAIL": "test@test-project.iam.gserviceaccount.com",
                "GCP_PRIVATE_KEY_ID": "test-key-id-123",
            },
        ),
        patch("gcsfs.GCSFileSystem") as mock_gcs_fs,
    ):
        mock_fs_instance: MagicMock = MagicMock()
        mock_gcs_fs.return_value = mock_fs_instance

        # Empty iterator
        file_data: list[DestinationFileData] = []

        # Should not raise any errors
        CloudGoogle.to_filesystem_gcs(iter(file_data))

        # Filesystem should be created but no files opened
        assert mock_gcs_fs.call_count == 1
        assert mock_fs_instance.open.call_count == 0


def test_to_filesystem_gcs_write_error() -> None:
    """Test to_filesystem_gcs when file write fails."""
    from unittest.mock import MagicMock, patch

    import pytest

    with (
        patch.dict(
            os.environ,
            {
                "GCP_PROJECT_ID": "test-project",
                "GCP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\ntest-key\\n-----END PRIVATE KEY-----",
                "GCP_CLIENT_EMAIL": "test@test-project.iam.gserviceaccount.com",
                "GCP_PRIVATE_KEY_ID": "test-key-id-123",
            },
        ),
        patch("gcsfs.GCSFileSystem") as mock_gcs_fs,
    ):
        mock_fs_instance: MagicMock = MagicMock()
        mock_file: MagicMock = MagicMock()
        mock_file.write.side_effect = OSError("Write failed")
        mock_fs_instance.open.return_value.__enter__.return_value = mock_file
        mock_gcs_fs.return_value = mock_fs_instance

        file_data: list[DestinationFileData] = [
            DestinationFileData(string="test", path="gs://bucket/file.json"),
        ]

        with pytest.raises(OSError, match="Write failed"):
            CloudGoogle.to_filesystem_gcs(iter(file_data))


def test_export_to_filesystem() -> None:
    """Test export_to_filesystem delegates to to_filesystem."""
    from unittest.mock import patch

    with patch.object(
        CloudGoogle,
        "to_filesystem",
        return_value="Success",
    ) as mock_to_fs:
        file_data: list[DestinationFileData] = [
            DestinationFileData(string="test", path="test.json"),
        ]
        result: str = CloudGoogle.export_to_filesystem(iter(file_data), "gs://bucket")

        mock_to_fs.assert_called_once()
        assert result == "Success"


def test_export_to_gcs() -> None:
    """Test export_to_gcs delegates to to_filesystem_gcs."""
    from unittest.mock import patch

    with patch.object(CloudGoogle, "to_filesystem_gcs") as mock_to_gcs:
        file_data: list[DestinationFileData] = [
            DestinationFileData(string="test", path="test.json"),
        ]
        CloudGoogle.export_to_gcs(iter(file_data))

        mock_to_gcs.assert_called_once()


def test_gcp_credentials_namedtuple() -> None:
    """Test GCPCredentials NamedTuple creation and access."""
    creds: GCPCredentials = GCPCredentials(
        project_id="test-project",
        private_key="test-key",
        client_email="test@example.com",
        private_key_id="test-key-id",
    )

    assert creds.project_id == "test-project"
    assert creds.private_key == "test-key"
    assert creds.client_email == "test@example.com"
    assert creds.private_key_id == "test-key-id"

    # Test with None values
    empty_creds: GCPCredentials = GCPCredentials(
        project_id=None,
        private_key=None,
        client_email=None,
        private_key_id=None,
    )

    assert empty_creds.project_id is None
    assert empty_creds.private_key is None
    assert empty_creds.client_email is None
    assert empty_creds.private_key_id is None


def test_clean_bucket_name_parametrized() -> None:
    """Parametrized test for clean_bucket_name with various inputs."""
    test_cases: list[tuple[str, str]] = [
        ("simple-bucket", "simple_bucket"),
        ("bucket-with-many-hyphens", "bucket_with_many_hyphens"),
        ("bucket_already_clean", "bucket_already_clean"),
        ("mixed-bucket_name-123", "mixed_bucket_name_123"),
        ("", ""),
        ("a-b-c-d-e-f", "a_b_c_d_e_f"),
    ]

    for bucket_name, expected in test_cases:
        assert CloudGoogle.clean_bucket_name(bucket_name) == expected


def test_strip_bucket_url_parametrized() -> None:
    """Parametrized test for strip_bucket_url with various inputs."""
    test_cases: list[tuple[str, str]] = [
        ("gs://bucket-name", "bucket_name"),
        ("gs://bucket_name", "bucket_name"),
        ("bucket-name", "bucket_name"),
        ("gs://complex-bucket-name-123", "complex_bucket_name_123"),
        ("", ""),
        ("no-prefix-bucket", "no_prefix_bucket"),
    ]

    for url, expected in test_cases:
        assert CloudGoogle.strip_bucket_url(url) == expected


def test_to_filesystem_gcs_with_various_file_types() -> None:
    """Test to_filesystem_gcs with different file types."""
    import json
    from unittest.mock import MagicMock, patch

    def _create_test_destination_file_data() -> Iterator[DestinationFileData]:
        """Helper function to create test DestinationFileData instances.

        Yields various test cases including JSON, CSV, and text files.
        """
        # JSON file
        json_data: dict[str, str | int] = {"test": "data", "count": 42}
        yield DestinationFileData(
            string=json.dumps(json_data),
            path="gs://test-bucket/data/test.json",
        )

        # CSV file
        csv_content: str = "name,age,city\nJohn,30,NYC\nJane,25,LA"
        yield DestinationFileData(
            string=csv_content,
            path="gs://test-bucket/data/users.csv",
        )

        # Text file
        yield DestinationFileData(
            string="This is a test file content",
            path="gs://test-bucket/logs/test.log",
        )

    with (
        patch.dict(
            os.environ,
            {
                "GCP_PROJECT_ID": "test-project",
                "GCP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\ntest-key\\n-----END PRIVATE KEY-----",
                "GCP_CLIENT_EMAIL": "test@test-project.iam.gserviceaccount.com",
                "GCP_PRIVATE_KEY_ID": "test-key-id-123",
            },
        ),
        patch("gcsfs.GCSFileSystem") as mock_gcs_fs,
    ):
        mock_fs_instance: MagicMock = MagicMock()
        mock_file: MagicMock = MagicMock()
        mock_fs_instance.open.return_value.__enter__.return_value = mock_file
        mock_gcs_fs.return_value = mock_fs_instance

        # Execute with test data
        CloudGoogle.to_filesystem_gcs(_create_test_destination_file_data())

        # Verify all files were processed
        assert mock_fs_instance.open.call_count == 3
        assert mock_file.write.call_count == 3


def test_to_filesystem_creates_directory_for_local_path() -> None:
    """Test that to_filesystem creates directories for local paths."""
    import tempfile

    # Create a list to hold the data passed to to_filesystem_local
    captured_data: list[DestinationFileData] = []

    def mock_to_filesystem_local(
        *,
        destination_file_data: Iterator[DestinationFileData],
    ) -> None:
        # Consume the iterator to capture the data
        captured_data.extend(list(destination_file_data))

    # Store the original function
    original_func: object = globals()["to_filesystem_local"]

    with tempfile.TemporaryDirectory() as tmpdir:
        # Replace the function in globals
        globals()["to_filesystem_local"] = mock_to_filesystem_local

        try:
            # Use temp directory path
            local_path: Path = Path(tmpdir) / "local" / "path" / "to" / "dir"

            file_data: list[DestinationFileData] = [
                DestinationFileData(string="test", path="test.json"),
            ]
            CloudGoogle.to_filesystem(iter(file_data), str(local_path))

            # Verify directory creation
            assert local_path.exists()
            assert local_path.is_dir()

            # Verify to_filesystem_local was called
            assert len(captured_data) == 1

        finally:
            # Restore the original function
            globals()["to_filesystem_local"] = original_func
