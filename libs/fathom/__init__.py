"""Fathom domain models and API adapter.

Models cover the recording webhook payload (the *push* path). The client +
``from_sdk`` helpers cover the *pull* path: listing recordings via the official
``fathom-python`` SDK and reshaping them into the same ``Webhook`` model so the
webhook → Attio transform can be reused for backfill.
"""

from libs.fathom.client import api_key_scope, get_client, iter_meetings
from libs.fathom.errors import FathomAuthError, FathomError
from libs.fathom.from_sdk import webhook_from_sdk_meeting
from libs.fathom.models import (
    ActionItem,
    Assignee,
    CalendarInvitee,
    CrmCompany,
    CrmContact,
    CrmMatches,
    DefaultSummary,
    RecordedBy,
    TranscriptMessage,
    TranscriptSpeaker,
    Webhook,
)

__all__ = [
    "ActionItem",
    "Assignee",
    "CalendarInvitee",
    "CrmCompany",
    "CrmContact",
    "CrmMatches",
    "DefaultSummary",
    "FathomAuthError",
    "FathomError",
    "RecordedBy",
    "TranscriptMessage",
    "TranscriptSpeaker",
    "Webhook",
    "api_key_scope",
    "get_client",
    "iter_meetings",
    "webhook_from_sdk_meeting",
]
