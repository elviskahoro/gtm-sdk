"""Composable webhook drop-filter base class.

Each source's ``src/<source>/webhook/*.py`` may declare a list of
``WebhookFilter`` subclasses that decide whether a payload should be
dropped from the Attio export path. The shared base lives here so the
pattern stays consistent across sources (octolens, rb2b, ...).

Filters apply to the Attio path only. The ETL and raw-passthrough paths
always land every webhook in GCS so re-processing can recover dropped
events without re-fetching from the source.

A source composes its own discriminated-union ``WebhookFilters``
``RootModel`` over its concrete subclasses — see
``src/octolens/webhook/mention.py`` and ``src/rb2b/webhook/visit.py``
for the two reference call sites.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class WebhookFilter(BaseModel):
    """Base class for composable webhook drop-filters.

    Subclasses must declare a unique ``type: Literal["..."]`` discriminator
    so a per-source ``RootModel`` can serialize/deserialize a heterogeneous
    list. ``should_exclude`` returns ``True`` to drop the webhook from the
    Attio export path.
    """

    name: str

    def should_exclude(self, webhook: Any) -> bool:
        raise NotImplementedError
