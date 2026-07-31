"""Regression coverage for the minimal GCP ETL Modal image dependencies."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_gcp_etl_image_installs_attio_for_imported_source_adapters() -> None:
    """The ETL handler imports Octolens, which imports shared Attio helpers."""
    source = (REPO_ROOT / "webhooks" / "export_to_gcp_etl.py").read_text()

    assert '"attio>=0.22.8"' in source  # trunk-ignore(ruff/S101)
