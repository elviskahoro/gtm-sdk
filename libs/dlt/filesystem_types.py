"""Adapter-local filesystem types for ``libs.dlt``.

Decouples the DLT adapter from ``libs.filesystem`` so the two stay independent
Bazel ownership units (no cross-adapter import edge — see
``tests/architecture/test_import_boundaries.py``).

``WritableFile`` is the structural protocol the adapter's write entrypoints type
their ``destination_file_data`` parameter against. The concrete
``DestinationFileData`` NamedTuple owned by ``libs.filesystem.files`` (and the
one orchestration constructs in ``src``/``webhooks``) satisfies it structurally
— same ``string``/``path`` attributes — so it crosses the boundary as a value at
call time, never as an import. ``DestinationFileData`` is mirrored here as a
concrete, constructible carrier for the adapter's own fixtures/tests so the
module never needs to import the sibling adapter.
"""

from __future__ import annotations

from typing import NamedTuple, Protocol


class WritableFile(Protocol):
    """Structural type for a writable destination file.

    Any object exposing readable ``string``/``path`` string attributes
    satisfies this; the concrete ``DestinationFileData`` in
    ``libs.filesystem.files`` does, so orchestration keeps passing it unchanged
    with no runtime adapter. The members are declared as read-only properties
    so a ``NamedTuple`` (whose fields are immutable) satisfies the protocol
    under pyright — the adapter only ever reads these attributes.
    """

    @property
    def string(self) -> str: ...

    @property
    def path(self) -> str: ...


class DestinationFileData(NamedTuple):
    """Concrete writable-file carrier mirroring ``libs.filesystem.files``.

    Structural twin of the orchestration-owned ``DestinationFileData``: kept
    here so ``libs.dlt`` can build fixtures without importing ``libs.filesystem``.
    """

    string: str
    path: str
