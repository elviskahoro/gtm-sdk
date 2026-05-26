"""One-off bootstrap: add the ``source`` select attribute to ``tracking_events``.

ai-ztm — the canonical writer in ``libs/attio/values.py`` and the lifecycle
writer in ``libs/attio/tracking_events.py`` both emit ``source`` so Attio views
can filter rows by emitter (rb2b / caldotcom / form / ...) without parsing the
``external_id`` prefix. ``ensure_select_options`` JIT-seeds the option titles
on first write but cannot create the underlying attribute — that has to exist
on the workspace before any source-bearing write succeeds, otherwise Attio
400s with "Cannot find attribute with slug source".

Usage::

    infisical run --env=dev -- uv run python scripts/tracking-events-source-attr-bootstrap.py --preview
    infisical run --env=dev -- uv run python scripts/tracking-events-source-attr-bootstrap.py --apply

Idempotent. Safe to re-run. Once the attribute exists, this script is a no-op
and can be deleted in a follow-up.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Anchor on the script's own directory so paths resolve regardless of CWD
# (per repo CLAUDE.md path-anchoring rule). ``uv run path/to/script.py`` does
# NOT chdir into the script's folder.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.attio.attributes import create_attribute  # noqa: E402

OBJECT_API_SLUG = "tracking_events"
ATTR_API_SLUG = "source"
ATTR_TITLE = "Source"
ATTR_DESCRIPTION = (
    "Emitter slug for the row (rb2b, caldotcom, form, ...). "
    "Used to filter tracking_events views by source without parsing "
    "the external_id prefix. Vocabulary grows JIT — new emitters "
    "self-register their option title on first write."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preview",
        action="store_true",
        help="Print what would happen; no writes.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Create the source attribute if missing.",
    )
    args = parser.parse_args()
    apply = bool(args.apply)

    result = create_attribute(
        target_object=OBJECT_API_SLUG,
        title=ATTR_TITLE,
        api_slug=ATTR_API_SLUG,
        attribute_type="select",
        description=ATTR_DESCRIPTION,
        is_multiselect=False,
        is_required=False,
        is_unique=False,
        apply=apply,
    )

    if result.attribute_created:
        status = "created"
    elif result.attribute_exists:
        status = "exists (skip)"
    else:
        status = "would-create"
    print(f"[attribute] {OBJECT_API_SLUG}.{ATTR_API_SLUG}  select  {status}")

    if not apply and not result.attribute_exists:
        print("Run with --apply to execute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
