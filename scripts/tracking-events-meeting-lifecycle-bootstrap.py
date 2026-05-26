"""Idempotent attribute bootstrap for the meeting-lifecycle row model on tracking_events.

Surfaces cal.com meeting lifecycle as a per-meeting MUTATING row on the
existing ``tracking_events`` object -- one row per meeting (keyed by
``external_id = canonical_meeting_uid(host, start)``), patched in place as the
meeting transitions through states. See
``design/backlog-202605251625-meeting_state_attrs_on_tracking_events-spec-01.md``.

The schema audit (2026-05-25) found prod's ``tracking_events`` already has
every slug the dispatcher needs (``details``, ``no_show``, ``people``, plus the
existing baseline). Dev is behind prod -- it has ``contact`` instead of
``people`` and lacks ``details`` / ``no_show``. This script brings dev in line
with prod and seeds the new select options on both.

What this script does in each workspace:

1. Add the ``people`` record-reference attribute (allowed_objects=["people"]).
   No-op on prod where it already exists.
2. Add the ``details`` text attribute. No-op on prod.
3. Add the ``no_show`` checkbox attribute. No-op on prod.
4. Seed ``event_type`` option ``calcom_meeting`` (single namespace value used by
   every meeting-lifecycle row).
5. Seed ``event_subtype`` options: ``scheduled``, ``cancelled``, ``rescheduled``,
   ``no_show_attendee``, ``no_show_host``, ``completed``.

What this script deliberately does NOT do:

- Touch dev's existing ``contact`` slug. Other dispatchers (rb2b) write to it;
  migrating rb2b to ``people`` is out of scope. After this script, the
  lifecycle dispatcher uses ``people`` and rb2b continues writing ``contact``.
- Delete the orphaned ``meeting_status`` attribute on dev (left over from an
  earlier design iteration before the per-meeting model was settled). Filed as
  follow-up; Attio attribute deletion via API has edge cases not worth the risk.
- Add per-event-type slugs (rating, cancellation_reason, etc.). All that
  context lives in ``body`` (raw JSON) and ``details`` (cumulative human-readable
  history) per the spec.

Usage:
    uv run python scripts/tracking-events-meeting-lifecycle-bootstrap.py --preview
    uv run python scripts/tracking-events-meeting-lifecycle-bootstrap.py --apply

Run against dev first (``infisical run ... --env=dev -- ...``), verify the new
slugs are visible in Attio, then run against prod.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from libs.attio.attributes import create_attribute, ensure_select_options

SCRIPT_DIR = Path(__file__).resolve().parent

TARGET = "tracking_events"

# Single event_type option carrying every meeting-lifecycle row. Keeps the
# meeting namespace separated from rb2b_visit / form_submission etc. on the
# same object. The dispatcher writes this value verbatim on every row.
_EVENT_TYPE_OPTIONS: tuple[str, ...] = ("calcom_meeting",)

# Closed vocabulary for the per-row state. Extending requires re-running this
# bootstrap. Keep in sync with the dispatcher mapping table in the spec.
_EVENT_SUBTYPE_OPTIONS: tuple[str, ...] = (
    "scheduled",
    "cancelled",
    "rescheduled",
    "no_show_attendee",
    "no_show_host",
    "completed",
)

# (slug, title, type, extra) -- ``extra`` carries the record-reference's
# allowed_objects when applicable. Order is illustrative; attributes are
# independent and Attio accepts them in any order.
_ATTRIBUTES: tuple[tuple[str, str, str, dict[str, object] | None], ...] = (
    ("people", "People", "record-reference", {"allowed_objects": ["people"]}),
    ("details", "Details", "text", None),
    ("no_show", "No Show", "checkbox", None),
)


def main(apply: bool) -> int:
    results: list[tuple[str, str]] = []

    # Create the attributes first; ensure_select_options on event_type /
    # event_subtype below works regardless of whether the lifecycle attributes
    # exist, but doing creates first keeps the printed result order matching
    # the order an operator would read the schema in.
    for slug, title, attr_type, extra in _ATTRIBUTES:
        allowed = (extra or {}).get("allowed_objects")
        r = create_attribute(
            target_object=TARGET,
            title=title,
            api_slug=slug,
            attribute_type=attr_type,
            is_multiselect=False,
            allowed_objects=allowed,  # type: ignore[arg-type]
            apply=apply,
        )
        results.append(
            (
                slug,
                "created"
                if r.attribute_created
                else "exists"
                if r.attribute_exists
                else "would-create",
            ),
        )

    # Seed event_type:calcom_meeting (additive; existing options untouched).
    added_event_types = ensure_select_options(
        target_object=TARGET,
        attribute_slug="event_type",
        options=list(_EVENT_TYPE_OPTIONS) if apply else [],
    )
    results.append(
        (
            "event_type:calcom_meeting",
            f"applied ({len(added_event_types)} new)" if apply else "would-apply",
        ),
    )

    # Seed event_subtype lifecycle states (additive).
    added_subtypes = ensure_select_options(
        target_object=TARGET,
        attribute_slug="event_subtype",
        options=list(_EVENT_SUBTYPE_OPTIONS) if apply else [],
    )
    results.append(
        (
            "event_subtype:lifecycle states",
            f"applied ({len(added_subtypes)} new)" if apply else "would-apply",
        ),
    )

    for slug, status in results:
        print(f"  {slug:32s} {status}")
    print(f"\nMode: {'apply' if apply else 'preview'}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--preview", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    raise SystemExit(main(apply=args.apply))
