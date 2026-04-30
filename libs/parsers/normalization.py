from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SupportsModelDump(Protocol):
    def model_dump(self, *, mode: str = "python") -> dict[str, Any]: ...


def normalize_mapping_payload(value: object, *, mode: str = "json") -> dict[str, Any]:
    if isinstance(value, SupportsModelDump):
        return value.model_dump(mode=mode)

    if isinstance(value, Mapping):
        return dict(value)

    raise TypeError(f"Expected mapping-like payload, got {type(value).__name__}")
