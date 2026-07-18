from __future__ import annotations

import argparse
import csv
import zipfile
from collections.abc import Iterable
from pathlib import Path


ARCHIVE_NAME = "pytest-deps.zip"
PATH_FILE_NAME = "pytest-deps.pth"
PACKABLE_SUFFIXES = frozenset({".py", ".pyi"})


def _record_paths(
    site_packages: Path,
    record: Path,
) -> list[Path] | None:
    paths: list[Path] = []
    with record.open(newline="") as handle:
        rows = csv.reader(handle)
        for row in rows:
            if not row:
                continue
            relative = Path(row[0])
            if relative.is_absolute() or ".." in relative.parts:
                return None
            if ".dist-info" in relative.parts[0]:
                continue
            path = site_packages / relative
            if not path.is_file():
                return None
            paths.append(path)
    return paths


def _uses_namespace_package(
    paths: Iterable[Path],
    site_packages: Path,
) -> bool:
    for path in paths:
        relative = path.relative_to(site_packages)
        parent = site_packages
        for part in relative.parts[:-1]:
            parent /= part
            if parent.is_dir() and not (parent / "__init__.py").is_file():
                return True
    return False


def _is_packable(
    paths: Iterable[Path],
    site_packages: Path,
) -> bool:
    paths = list(paths)
    return (
        bool(paths)
        and not _uses_namespace_package(paths, site_packages)
        and all(
            path.name == "py.typed" or path.suffix in PACKABLE_SUFFIXES
            for path in paths
        )
    )


def _remove_empty_parents(
    path: Path,
    site_packages: Path,
) -> None:
    parent = path.parent
    while parent != site_packages:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def pack_site_packages(site_packages: Path) -> dict[str, int]:
    archive = site_packages / ARCHIVE_NAME
    packed_paths: dict[str, Path] = {}
    archived_distributions = 0

    for record in sorted(site_packages.glob("*.dist-info/RECORD")):
        paths = _record_paths(site_packages, record)
        if paths is None or not _is_packable(paths, site_packages):
            continue
        relative_paths = [path.relative_to(site_packages).as_posix() for path in paths]
        if any(relative in packed_paths for relative in relative_paths):
            continue
        packed_paths.update(zip(relative_paths, paths, strict=True))
        archived_distributions += 1

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as packed:
        for relative, path in sorted(packed_paths.items()):
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            packed.writestr(info, path.read_bytes())

    for path in packed_paths.values():
        path.unlink()
        _remove_empty_parents(path, site_packages)

    (site_packages / PATH_FILE_NAME).write_text(f"{archive.as_posix()}\n")
    return {
        "archived_distributions": archived_distributions,
        "archived_files": len(packed_paths),
    }


def _site_packages(venv: Path) -> Path:
    candidates = sorted((venv / "lib").glob("python*/site-packages"))
    if len(candidates) != 1:
        raise ValueError(
            f"expected one site-packages directory under {venv}, found {candidates}",
        )
    return candidates[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("venv", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    site_packages = _site_packages(args.venv)
    stats = pack_site_packages(site_packages)
    archive = site_packages / ARCHIVE_NAME
    print(
        "Packed pytest dependency checkpoint: "
        f"{stats['archived_distributions']} distributions, "
        f"{stats['archived_files']} files, {archive.stat().st_size} bytes",
    )


if __name__ == "__main__":
    main()
