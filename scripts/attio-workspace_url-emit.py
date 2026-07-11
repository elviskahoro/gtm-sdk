#!/usr/bin/env -S uv run python
"""Emit shareable Attio inspection URLs for the injected ``ATTIO_API_KEY``.

Resolves the token's workspace slug from ``GET /v2/self`` (reusing
``libs.attio.preflight.fetch_token_scopes``) and turns it into ``app.attio.com``
links: the workspace root, per-object list views, and — with ``--record-id`` —
an individual record. Reusable from CI/CD or by hand to generate links to the
workspace / its objects.

The workspace is whichever one the injected ``ATTIO_API_KEY`` authenticates
against, so the Infisical environment is chosen at the ``infisical run --env=``
layer — this script has no ``--env`` flag of its own. Run it under
``infisical run`` so the key is present in the process environment.

Attio URL shapes (note the plural/singular split):

  * Workspace root:  https://app.attio.com/<slug>
  * Object list view (PLURAL slug + /view/):
        https://app.attio.com/<slug>/companies/view/
  * Individual record (SINGULAR slug):
        https://app.attio.com/<slug>/company/<record_id>

``--object`` takes the plural list slug (e.g. ``companies``). For a record URL,
the plural is mapped to its singular form for the standard objects; a custom
object falls back to a best-effort trailing-``s`` strip, which may be wrong —
prefer the ``web_url`` Attio returns on the record itself in that case.

Usage:

    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=<dev|prod> -- scripts/attio-workspace_url-emit.py
    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=<dev|prod> -- scripts/attio-workspace_url-emit.py --json
    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=<dev|prod> -- scripts/attio-workspace_url-emit.py --object companies
    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=<dev|prod> -- scripts/attio-workspace_url-emit.py --object people --record-id <uuid>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.attio.errors import AttioError  # noqa: E402
from libs.attio.preflight import fetch_token_scopes  # noqa: E402
from scripts.lib.env import clean_env, infisical_run_example  # noqa: E402

_ATTIO_APP_BASE = "https://app.attio.com"

# Attio uses the PLURAL object slug for list-view URLs but the SINGULAR form in
# an individual record's path (see module docstring). This maps the plural list
# slug an operator passes to ``--object`` to the singular used in a record URL,
# for the standard system objects. Custom objects are not in this map and fall
# back to a trailing-``s`` strip in ``build_record_url``.
STANDARD_OBJECTS_PLURAL_TO_SINGULAR: dict[str, str] = {
    "companies": "company",
    "people": "person",
    "deals": "deal",
    "users": "user",
    "workspaces": "workspace",
}


def build_workspace_url(slug: str) -> str:
    """Return the workspace root URL for ``slug``."""
    return f"{_ATTIO_APP_BASE}/{slug}"


def build_object_list_url(slug: str, object_slug: str) -> str:
    """Return the list-view URL for ``object_slug`` (plural) in ``slug``."""
    return f"{_ATTIO_APP_BASE}/{slug}/{object_slug}/view/"


def _singularize_object_slug(object_slug: str) -> str:
    """Map a plural list slug to the singular used in a record URL.

    Standard objects use the explicit map; custom objects fall back to a
    best-effort trailing-``s`` strip (documented as unreliable).
    """
    mapped = STANDARD_OBJECTS_PLURAL_TO_SINGULAR.get(object_slug)
    if mapped is not None:
        return mapped
    if object_slug.endswith("s") and len(object_slug) > 1:
        return object_slug[:-1]
    return object_slug


def build_record_url(slug: str, object_slug: str, record_id: str) -> str:
    """Return the individual-record URL (singular object slug) in ``slug``."""
    singular = _singularize_object_slug(object_slug)
    return f"{_ATTIO_APP_BASE}/{slug}/{singular}/{record_id}"


def build_standard_object_urls(slug: str) -> dict[str, str]:
    """Return ``{object_slug: list_view_url}`` for the standard objects."""
    return {
        object_slug: build_object_list_url(slug, object_slug)
        for object_slug in STANDARD_OBJECTS_PLURAL_TO_SINGULAR
    }


def resolve_workspace_slug() -> str:
    """Return the injected token's workspace slug from ``GET /v2/self``.

    Raises :class:`ValueError` when the token is inactive or the response omits
    a slug, so ``main`` can surface a clean, non-traceback error.
    """
    active, _scopes, workspace_slug = fetch_token_scopes()
    if not active:
        raise ValueError(
            "Attio token is inactive (GET /v2/self reported active=false). "
            "Regenerate the token in Attio -> Settings -> Developers and update "
            "it in Infisical.",
        )
    if not workspace_slug:
        raise ValueError(
            "GET /v2/self did not include a workspace_slug (is the token "
            "active and valid for a workspace?).",
        )
    return workspace_slug


def _render(
    slug: str,
    *,
    object_slug: str | None,
    record_id: str | None,
    json_output: bool,
) -> str:
    """Build the stdout payload for the resolved workspace ``slug``."""
    if object_slug is not None:
        url = (
            build_record_url(slug, object_slug, record_id)
            if record_id is not None
            else build_object_list_url(slug, object_slug)
        )
        if json_output:
            return json.dumps({"workspace_slug": slug, "url": url}, indent=2)
        return url

    base_url = build_workspace_url(slug)
    objects = build_standard_object_urls(slug)
    if json_output:
        return json.dumps(
            {"workspace_slug": slug, "base_url": base_url, "objects": objects},
            indent=2,
        )
    lines = [base_url, *(f"{name}: {url}" for name, url in objects.items())]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--object",
        dest="object_slug",
        default=None,
        help=(
            "Emit a single object's list-view URL. Pass the PLURAL list slug "
            "(e.g. companies, people). With --record-id, emits that record's "
            "URL instead (plural is mapped to singular for standard objects)."
        ),
    )
    parser.add_argument(
        "--record-id",
        dest="record_id",
        default=None,
        help="Record UUID for a single-record URL. Requires --object.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of human-readable text.",
    )
    args = parser.parse_args()

    if args.record_id is not None and args.object_slug is None:
        print("--record-id requires --object.", file=sys.stderr)
        return 2

    if not clean_env(os.environ.get("ATTIO_API_KEY")):
        print(
            "ATTIO_API_KEY is not set. Run under infisical run so the key is "
            "injected:\n"
            f"  {infisical_run_example('scripts/attio-workspace_url-emit.py')}",
            file=sys.stderr,
        )
        return 2

    try:
        slug = resolve_workspace_slug()
    except (AttioError, ValueError) as exc:
        print(f"attio workspace-url failed: {exc}", file=sys.stderr)
        return 1

    output = _render(
        slug,
        object_slug=args.object_slug,
        record_id=args.record_id,
        json_output=args.json,
    )
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
