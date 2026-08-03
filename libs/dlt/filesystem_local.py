from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from libs.dlt.filesystem_types import WritableFile


def to_filesystem_local(
    destination_file_data: Iterator[WritableFile],
) -> None:
    for file_data in destination_file_data:
        file_path: Path = Path(file_data.path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open(
            mode="w+",
        ) as f:
            f.write(
                file_data.string,
            )
