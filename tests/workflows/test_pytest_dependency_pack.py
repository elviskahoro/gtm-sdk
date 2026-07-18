import csv
import runpy
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import cast


PACKER = (
    Path(__file__).parents[2]
    / ".github"
    / "workflows"
    / "ci"
    / "pytest_dependency_pack.py"
)


def _write_distribution(
    site_packages: Path,
    name: str,
    files: dict[str, bytes],
) -> None:
    dist_info = site_packages / f"{name}-1.0.dist-info"
    dist_info.mkdir(parents=True)
    rows: list[list[str]] = []
    for relative, contents in files.items():
        path = site_packages / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        rows.append([relative, "", ""])
    record = dist_info / "RECORD"
    rows.append([record.relative_to(site_packages).as_posix(), "", ""])
    with record.open("w", newline="") as handle:
        csv.writer(handle).writerows(rows)


def _pack(site_packages: Path) -> dict[str, int]:
    namespace = runpy.run_path(str(PACKER))
    pack_site_packages = namespace["pack_site_packages"]
    return cast(dict[str, int], pack_site_packages(site_packages))


def test_packer_archives_pure_python_and_preserves_metadata(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    _write_distribution(
        site_packages,
        "pure",
        {"pure/__init__.py": b"VALUE = 1\n", "pure/py.typed": b""},
    )

    stats = _pack(site_packages)

    archive = site_packages / "pytest-deps.zip"
    with zipfile.ZipFile(archive) as packed:
        assert packed.namelist() == ["pure/__init__.py", "pure/py.typed"]
        assert all(
            item.compress_type == zipfile.ZIP_STORED for item in packed.infolist()
        )
        assert all(
            item.date_time == (1980, 1, 1, 0, 0, 0) for item in packed.infolist()
        )
    assert not (site_packages / "pure").exists()
    assert (site_packages / "pure-1.0.dist-info" / "RECORD").is_file()
    assert (site_packages / "pytest-deps.pth").read_text() == (
        f"{archive.as_posix()}\n"
    )
    subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import site; "
                f"site.addsitedir({str(site_packages)!r}); "
                "import pure; "
                "assert pure.VALUE == 1"
            ),
        ],
        check=True,
    )
    assert stats == {"archived_distributions": 1, "archived_files": 2}


def test_packer_leaves_native_and_resource_distributions_expanded(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    _write_distribution(
        site_packages,
        "native",
        {"native/__init__.py": b"", "native/extension.so": b"native"},
    )
    _write_distribution(
        site_packages,
        "resourceful",
        {"resourceful/__init__.py": b"", "resourceful/schema.json": b"{}"},
    )

    stats = _pack(site_packages)

    with zipfile.ZipFile(site_packages / "pytest-deps.zip") as packed:
        assert packed.namelist() == []
    assert (site_packages / "native" / "extension.so").is_file()
    assert (site_packages / "resourceful" / "schema.json").is_file()
    assert stats == {"archived_distributions": 0, "archived_files": 0}


def test_packer_leaves_namespace_packages_expanded(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    _write_distribution(
        site_packages,
        "namespace_one",
        {"shared_namespace/one.py": b"VALUE = 1\n"},
    )
    _write_distribution(
        site_packages,
        "namespace_two",
        {"shared_namespace/two.py": b"VALUE = 2\n"},
    )

    stats = _pack(site_packages)

    with zipfile.ZipFile(site_packages / "pytest-deps.zip") as packed:
        assert packed.namelist() == []
    assert (site_packages / "shared_namespace" / "one.py").is_file()
    assert (site_packages / "shared_namespace" / "two.py").is_file()
    assert stats == {"archived_distributions": 0, "archived_files": 0}
