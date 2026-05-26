"""Idempotent attribute bootstrap for the ``tracking_events`` Attio object.

Covers two concerns on the same object:

A. Cross-emitter ``source`` select attribute (ai-ztm). The canonical writer in
   ``libs/attio/values.py`` and the lifecycle writer in
   ``libs/attio/tracking_events.py`` both emit ``source`` so Attio views can
   filter rows by emitter (rb2b / caldotcom / form / ...) without parsing the
   ``external_id`` prefix. ``ensure_select_options`` JIT-seeds option titles
   on first write but cannot create the attribute itself -- without it Attio
   400s with "Cannot find attribute with slug source".

B. Meeting-lifecycle row model. Surfaces cal.com meeting lifecycle as a
   per-meeting MUTATING row keyed by
   ``external_id = canonical_meeting_uid(host, start)``, patched in place as
   the meeting transitions through states. See
   ``design/backlog-202605251625-meeting_state_attrs_on_tracking_events-spec-01.md``.

The schema audit (2026-05-25) found prod's ``tracking_events`` already has
every lifecycle slug the dispatcher needs (``details``, ``no_show``,
``people``, plus the existing baseline). Dev is behind prod -- it has
``contact`` instead of ``people`` and lacks ``details`` / ``no_show``. This
script brings dev in line with prod and seeds the new select options on both.

What this script does in each workspace:

1. Add the ``source`` select attribute (cross-emitter filter). Option titles
   grow JIT on first write per emitter -- this only creates the attribute.
2. Add the ``people`` record-reference attribute (allowed_objects=["people"]).
   No-op on prod where it already exists.
3. Add the ``details`` text attribute. No-op on prod.
4. Add the ``no_show`` checkbox attribute. No-op on prod.
5. Seed ``event_type`` option ``calcom_meeting`` (single namespace value used by
   every meeting-lifecycle row).
6. Seed ``event_subtype`` options: ``scheduled``, ``cancelled``, ``rescheduled``,
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
    uv run python scripts/attio-bootstrap-tracking_events.py --preview
    uv run python scripts/attio-bootstrap-tracking_events.py --apply

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
# allowed_objects or a select attribute's description when applicable. Order
# is illustrative; attributes are independent and Attio accepts them in any
# order.
_SOURCE_DESCRIPTION = (
    "Emitter slug for the row (rb2b, caldotcom, form, ...). "
    "Used to filter tracking_events views by source without parsing "
    "the external_id prefix. Vocabulary grows JIT — new emitters "
    "self-register their option title on first write."
)
_ATTRIBUTES: tuple[tuple[str, str, str, dict[str, object] | None], ...] = (
    ("source", "Source", "select", {"description": _SOURCE_DESCRIPTION}),
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
        extra_d = extra or {}
        allowed = extra_d.get("allowed_objects")
        description = extra_d.get("description")
        r = create_attribute(
            target_object=TARGET,
            title=title,
            api_slug=slug,
            attribute_type=attr_type,
            is_multiselect=False,
            allowed_objects=allowed,  # type: ignore[arg-type]
            description=description,  # type: ignore[arg-type]
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
