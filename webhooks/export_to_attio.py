# trunk-ignore-all(ruff/PGH003,trunk/ignore-does-nothing)
from __future__ import annotations

from pathlib import Path

import modal
from modal import Image

# trunk-ignore-begin(ruff/F401,ruff/I001,pyright/reportUnusedImport)
# fmt: off
from src.fathom.webhook.message import (
    Webhook as FathomMessageWebhook,
)
from src.fathom.webhook.call import (
    Webhook as FathomCallWebhook,
)
from src.octolens.webhook import (
    Webhook as OctolensWebhook,
)
from src.rb2b.webhook.visit import (
    Webhook as Rb2bVisitWebhook,
)
from src.caldotcom.webhook.booking import (
    Webhook as CaldotcomBookingWebhook,
)
# fmt: on
# trunk-ignore-end(ruff/F401,ruff/I001,pyright/reportUnusedImport)

from libs.attio.meetings import find_or_create_meeting
from libs.attio.models import (
    MeetingExternalRef,
    MeetingInput,
    MeetingParticipantInput,
)


class WebhookModel(FathomCallWebhook):  # type: ignore # trunk-ignore(ruff/F821)
    pass


WebhookModel.model_rebuild()


image: Image = modal.Image.debian_slim().uv_pip_install(
    "attio>=0.21.2",
    "fastapi[standard]",
    "orjson",
    "uuid7",
)
image = image.add_local_python_source(
    *[
        "libs",
        "src",
    ],
)
app = modal.App(name="export-to-attio", image=image)


def _export(webhook: WebhookModel) -> str:
    # Pass 1: inline Fathom-call → Attio Meeting. Pass 2 extracts via dispatcher.
    if not webhook.calendar_invitees or not webhook.recording_id:
        return (
            "Fathom call payload is not exportable to Attio "
            "(no attendees or recording_id)"
        )

    description: str = (
        webhook.default_summary.markdown_formatted
        if webhook.default_summary
        else (webhook.meeting_title or webhook.title)
    )

    meeting = MeetingInput(
        external_ref=MeetingExternalRef(
            ical_uid=f"fathom-call-{webhook.recording_id}",
            provider="google",
            is_recurring=False,
        ),
        title=webhook.meeting_title or webhook.title,
        description=description,
        start=webhook.scheduled_start_time,
        end=webhook.scheduled_end_time,
        is_all_day=False,
        participants=[
            MeetingParticipantInput(
                email_address=inv.email,
                is_organizer=(inv.email == webhook.recorded_by.email),
            )
            for inv in webhook.calendar_invitees
        ],
    )
    envelope = find_or_create_meeting(meeting)
    return envelope.model_dump_json()


@app.function(
    secrets=[modal.Secret.from_name("attio")],
    region="us-east-1",
    enable_memory_snapshot=False,
)
@modal.fastapi_endpoint(method="POST", docs=True)
@modal.concurrent(max_inputs=1000)
def web(webhook: WebhookModel) -> str:
    return _export(webhook)


@app.local_entrypoint()
def local(input_file: str) -> None:
    import orjson

    raw = Path(input_file).read_bytes()
    payload = orjson.loads(raw)
    webhook = WebhookModel.model_validate(payload)
    print(_export(webhook))
