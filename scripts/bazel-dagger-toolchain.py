#!/usr/bin/env -S uv run python
# ruff: noqa: N999 -- direct executable scripts use hyphenated filenames.
"""Provision the pinned Linux tools consumed by the Bazel Dagger controller.

The controller always runs a linux/arm64 container, including when started on a
Mac.  Keeping these host-mounted inputs here makes CI and local validation use
the same checked binaries rather than accidentally mounting a host Bazel.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_TMP_DIR = REPO_ROOT / "tmp" / "bazel-dagger-downloads"
BAZEL_VERSION = "9.2.0"
BAZEL_SHA256 = "049dd21f40ad979db11c3ee68c96a42ce75f1185e69ac61ab20de1501427a410"
BAZEL_URL = (
    f"https://releases.bazel.build/{BAZEL_VERSION}/release/"
    f"bazel-{BAZEL_VERSION}-linux-arm64"
)
BAZEL_DIFF_VERSION = "33.0.0"
BAZEL_DIFF_SHA256 = "4b649929970167f75a188d184cd07f00c83a82b363c70f74d3c1ad1f7cdefd51"
BAZEL_DIFF_URL = (
    "https://github.com/Tinder/bazel-diff/releases/download/"
    f"v{BAZEL_DIFF_VERSION}/bazel-diff_deploy.jar"
)


@dataclass(frozen=True)
class Toolchain:
    bazel: Path
    bazel_diff: Path


def _matches_checksum(path: Path, expected: str) -> bool:
    """Return whether a cached artifact matches its pinned SHA-256 digest."""
    if not path.is_file():
        return False
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest == expected


def _download(url: str, destination: Path) -> None:
    """Download one pinned artifact to a temporary or cache destination."""
    request = urlopen(  # noqa: S310 -- URLs and checksums are pinned above.  # nosec B310
        url,
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
    )
    with request, destination.open("wb") as output:
        shutil.copyfileobj(request, output)


def _ensure_artifact(
    *,
    directory: Path,
    name: str,
    url: str,
    checksum: str,
    executable: bool,
) -> Path:
    """Verify or atomically download one controller artifact."""
    destination = directory / name
    if _matches_checksum(destination, checksum):
        if executable:
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
        return destination

    DOWNLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=DOWNLOAD_TMP_DIR, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        _download(url, temporary_path)
        if not _matches_checksum(temporary_path, checksum):
            message = f"checksum verification failed for {url}"
            raise RuntimeError(message)
        if executable:
            temporary_path.chmod(temporary_path.stat().st_mode | stat.S_IXUSR)
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination


def ensure_toolchain(directory: Path) -> Toolchain:
    """Return verified controller inputs, repairing incomplete cache entries."""
    directory.mkdir(parents=True, exist_ok=True)
    return Toolchain(
        bazel=_ensure_artifact(
            directory=directory,
            name="bazel",
            url=BAZEL_URL,
            checksum=BAZEL_SHA256,
            executable=True,
        ),
        bazel_diff=_ensure_artifact(
            directory=directory,
            name="bazel-diff_deploy.jar",
            url=BAZEL_DIFF_URL,
            checksum=BAZEL_DIFF_SHA256,
            executable=False,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    """Parse the cache location, provision verified tools, and print their paths."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path.home() / ".bazel-dagger" / "toolchain",
        help="directory where verified controller inputs are cached",
    )
    args = parser.parse_args(argv)
    toolchain = ensure_toolchain(args.directory.expanduser())
    print(f"BAZEL_DAGGER_BINARY={toolchain.bazel}")
    print(f"BAZEL_DAGGER_DIFF_JAR={toolchain.bazel_diff}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
