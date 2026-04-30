from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from libs.granola.errors import ConfigError, SchemaError, SourceReadError

_VERSION_RE = re.compile(r"cache-v(\d+)\.json$")

LocalCachePayload = dict[str, Any]


def find_latest_cache_file(granola_dir: Path) -> Path:
    candidates: list[tuple[int, Path]] = []
    for path in granola_dir.glob("cache-v*.json"):
        match = _VERSION_RE.search(path.name)
        if match:
            candidates.append((int(match.group(1)), path))

    if not candidates:
        raise ConfigError(f"No cache-v*.json file found in {granola_dir}")

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def load_local_cache(path: Path) -> LocalCachePayload:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Local cache file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SourceReadError(f"Invalid JSON in local cache file: {path}") from exc

    if not isinstance(payload, dict):
        raise SchemaError("Expected local cache JSON object")
    return payload


def extract_local_records(
    payload: LocalCachePayload,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = payload.get("state")
    if not isinstance(state, dict):
        cache = payload.get("cache")
        if isinstance(cache, dict):
            state = cache.get("state")
    if not isinstance(state, dict):
        raise SchemaError("Missing 'state' object in local cache")

    documents = state.get("documents")
    transcripts = state.get("transcripts")

    if not isinstance(documents, dict) or not isinstance(transcripts, dict):
        raise SchemaError("Expected 'state.documents' and 'state.transcripts' maps")

    return documents, transcripts
