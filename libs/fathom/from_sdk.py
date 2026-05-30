"""Map a Fathom SDK ``Meeting`` into the webhook ``Webhook`` payload model.

The Fathom REST API and webhook share a schema, so the SDK's ``Meeting`` model
(``fathom_python.models.Meeting``) uses the *same field names and shapes* as our
:class:`libs.fathom.models.Webhook` — ``recording_id``, ``scheduled_start_time``,
``recorded_by``, ``default_summary``, ``action_items``, and even ``transcript`` /
``crm_matches`` all line up. The conversion is therefore faithful (no fields are
dropped); the only work is normalizing nullability, since the SDK marks several
fields ``Nullable`` that the webhook model requires non-null. Keeping the
mapping lossless lets the existing webhook → Attio transform
(``src/fathom/webhook/call.py``) be reused verbatim for backfill, with a single
source of truth for Fathom → Attio mapping.

Stays a pure data reshape with no cross-lib imports (per the libs hard rule):
it imports only its own models and accepts a duck-typed SDK item.
"""

from __future__ import annotations

from typing import Any

from libs.fathom.models import Webhook


def webhook_from_sdk_meeting(meeting: Any) -> Webhook:
    """Build a :class:`Webhook` from a ``fathom_python.models.Meeting``.

    ``meeting`` is anything with a Pydantic ``model_dump`` (the SDK item, or a
    test stub). Nullability gaps the webhook model can't accept are patched:

    * ``recorded_by.team`` — SDK ``Nullable``; webhook requires ``str``.
    * ``calendar_invitees`` — invitees with a null ``email`` can't anchor a
      meeting participant downstream, so they are dropped; remaining null
      string fields are coerced to ``""``.
    * ``default_summary`` — null ``template_name`` / ``markdown_formatted``
      coerced to ``""`` (note rendering tolerates empties).
    ``transcript`` and ``crm_matches`` are preserved as-is — their SDK shapes
    match the webhook model — even though the Attio transform does not read them.
    """
    data: dict[str, Any] = meeting.model_dump(mode="json")

    recorded_by = data.get("recorded_by")
    if isinstance(recorded_by, dict) and recorded_by.get("team") is None:
        recorded_by["team"] = ""

    invitees = data.get("calendar_invitees")
    if isinstance(invitees, list):
        cleaned: list[dict[str, Any]] = []
        for inv in invitees:
            if not isinstance(inv, dict) or not inv.get("email"):
                continue
            inv["name"] = inv.get("name") or ""
            inv["email_domain"] = inv.get("email_domain") or ""
            cleaned.append(inv)
        data["calendar_invitees"] = cleaned

    summary = data.get("default_summary")
    if isinstance(summary, dict):
        summary["template_name"] = summary.get("template_name") or ""
        summary["markdown_formatted"] = summary.get("markdown_formatted") or ""

    return Webhook.model_validate(data)
