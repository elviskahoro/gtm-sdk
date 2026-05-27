from __future__ import annotations

from libs.filesystem.refs import GCSObjectRef


def test_from_gcsfs_info_full_payload() -> None:
    info = {
        "bucket": "dlthub-devx-test",
        "name": "dlthub-devx-test/some/path.jsonl",
        "generation": "1700000000000001",
        "size": 4096,
        "md5Hash": "abcdef==",
        "etag": "etag-xyz",
        "timeCreated": "2026-05-26T12:34:56.789Z",
    }
    ref = GCSObjectRef.from_gcsfs_info(
        info=info,
        source_path="gs://dlthub-devx-test/some/path.jsonl",
    )
    assert ref.bucket == "dlthub-devx-test"
    assert ref.path == "some/path.jsonl"
    assert ref.gs_uri == "gs://dlthub-devx-test/some/path.jsonl"
    assert ref.generation == 1700000000000001
    assert ref.size_bytes == 4096
    assert ref.md5_hash == "abcdef=="
    assert ref.etag == "etag-xyz"
    assert ref.created_at is not None
    assert ref.created_at.year == 2026


def test_from_gcsfs_info_missing_optional_fields() -> None:
    info = {"name": "dlthub-devx-test/x.jsonl"}
    ref = GCSObjectRef.from_gcsfs_info(
        info=info,
        source_path="dlthub-devx-test/x.jsonl",
    )
    assert ref.bucket == "dlthub-devx-test"
    assert ref.path == "x.jsonl"
    assert ref.gs_uri == "gs://dlthub-devx-test/x.jsonl"
    assert ref.generation is None
    assert ref.size_bytes is None
    assert ref.md5_hash is None
    assert ref.etag is None
    assert ref.created_at is None


def test_from_gcsfs_info_source_path_without_scheme() -> None:
    info = {
        "generation": "42",
        "size": 10,
        "timeCreated": "2026-01-01T00:00:00Z",
    }
    ref = GCSObjectRef.from_gcsfs_info(
        info=info,
        source_path="bucket-x/key.json",
    )
    assert ref.bucket == "bucket-x"
    assert ref.path == "key.json"
    assert ref.gs_uri == "gs://bucket-x/key.json"
    assert ref.generation == 42
    assert ref.size_bytes == 10
    assert ref.created_at is not None


def test_from_gcsfs_info_malformed_int_fields_keep_other_fields() -> None:
    """If gcsfs returns a non-numeric value for `generation` or `size`
    (corruption, schema drift, exotic edge case), parsing must not raise —
    the write has ALREADY succeeded by the time this constructor runs, so
    a malformed readback can't be allowed to revert success into failure.
    """
    info = {
        "generation": "not-a-number",
        "size": [1, 2, 3],
        "md5Hash": "still-here==",
        "timeCreated": "2026-01-01T00:00:00Z",
    }
    ref = GCSObjectRef.from_gcsfs_info(
        info=info,
        source_path="b/k.json",
    )
    assert ref.generation is None
    assert ref.size_bytes is None
    assert ref.md5_hash == "still-here=="
    assert ref.created_at is not None


def test_from_gcsfs_info_invalid_timestamp_keeps_other_fields() -> None:
    """A malformed `timeCreated` must not blow up parsing — created_at goes
    to None, everything else still populates. Defensive because gcsfs has
    historically returned non-ISO strings on certain edge cases.
    """
    info = {
        "generation": "99",
        "timeCreated": "not-a-date",
    }
    ref = GCSObjectRef.from_gcsfs_info(
        info=info,
        source_path="b/k.json",
    )
    assert ref.generation == 99
    assert ref.created_at is None
