"""Minimal Hookdeck REST client for the webhook registry.

Pulls sources, destinations, and connections from the Hookdeck API and joins
them so we can resolve (Modal URL) → (Hookdeck source/destination/connection
IDs). HTTP-direct rather than shelling out to the `hookdeck` CLI so the
registry sync has no Node/CLI host dependency.

Auth: HOOKDECK_API_KEY in the environment (inject via Infisical).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

import requests

HOOKDECK_API_BASE: str = "https://api.hookdeck.com/2024-09-01"
PAGE_LIMIT: int = 250


@dataclass(frozen=True)
class Destination:
    id: str
    name: str
    url: str | None


@dataclass(frozen=True)
class Source:
    id: str
    name: str


@dataclass(frozen=True)
class Connection:
    id: str
    source_id: str
    destination_id: str


@dataclass(frozen=True)
class HookdeckInventory:
    """Joined view used by sync to look up wiring per Modal URL."""

    sources_by_id: dict[str, Source]
    destinations_by_id: dict[str, Destination]
    connections_by_destination_id: dict[str, Connection]

    def find_by_modal_url(
        self,
        modal_url: str,
    ) -> tuple[Source | None, Destination | None, Connection | None]:
        # Match on URL substring rather than exact equality — Hookdeck
        # sometimes appends path segments or query params to destinations.
        for dest in self.destinations_by_id.values():
            if dest.url and modal_url in dest.url:
                conn = self.connections_by_destination_id.get(dest.id)
                source = self.sources_by_id.get(conn.source_id) if conn else None
                return source, dest, conn
        return None, None, None


def _get(path: str, api_key: str) -> list[dict[str, Any]]:
    """GET a paginated Hookdeck collection. Returns the flat `models` list."""
    out: list[dict[str, Any]] = []
    next_cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": PAGE_LIMIT}
        if next_cursor:
            params["next"] = next_cursor
        resp = requests.get(
            f"{HOOKDECK_API_BASE}{path}",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        out.extend(body.get("models", []))
        pagination = body.get("pagination") or {}
        next_cursor = pagination.get("next")
        if not next_cursor:
            return out


def fetch_inventory() -> HookdeckInventory:
    api_key: str | None = os.environ.get("HOOKDECK_API_KEY")
    if not api_key:
        print(
            "HOOKDECK_API_KEY not set — Hookdeck wiring fields will be null. "
            "Run via `infisical run ... -- gtm webhook sync`.",
            file=sys.stderr,
        )
        return HookdeckInventory({}, {}, {})

    sources = {
        s["id"]: Source(id=s["id"], name=s.get("name", ""))
        for s in _get("/sources", api_key)
    }
    destinations = {
        d["id"]: Destination(
            id=d["id"],
            name=d.get("name", ""),
            url=d.get("url"),
        )
        for d in _get("/destinations", api_key)
    }
    connections_by_dest: dict[str, Connection] = {}
    for c in _get("/connections", api_key):
        # Hookdeck nests source/destination as objects on the connection.
        source_id = (c.get("source") or {}).get("id") or c.get("source_id")
        dest_id = (c.get("destination") or {}).get("id") or c.get("destination_id")
        if not (source_id and dest_id):
            continue
        connections_by_dest[dest_id] = Connection(
            id=c["id"],
            source_id=source_id,
            destination_id=dest_id,
        )
    return HookdeckInventory(
        sources_by_id=sources,
        destinations_by_id=destinations,
        connections_by_destination_id=connections_by_dest,
    )
