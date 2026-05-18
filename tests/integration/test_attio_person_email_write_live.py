from __future__ import annotations

import time

from libs.attio.models import PersonInput
from libs.attio.people import add_person


def test_attio_person_create_persists_email_addresses_wrapped_shape(
    attio_api_key: str,  # noqa: ARG001 — fixture handles credential skip
) -> None:
    # Regression guard for the 5ac70af disabling — the wrapped
    # `[{"email_address": "..."}]` shape must round-trip through Attio.
    # See design/backlog-202605172107-attio_reenable_email_addresses_writer-prompt.md.
    #
    # Use `example.com` rather than `example.test` — Attio's email validation
    # rejects RFC-2606 reserved TLDs with the misleading error
    # `An invalid value was passed to attribute with slug "email_addresses"`,
    # which is what fooled the 5ac70af author into thinking the shape was wrong.
    # See libs/attio/values.py::format_email_addresses_for_write for the same note.
    email = f"probe+{int(time.time())}@example.com"
    envelope = add_person(
        PersonInput(
            email=email,
            first_name="Probe",
            last_name="Test",
        ),
    )
    assert envelope.success, envelope
    assert envelope.record_id, envelope
    person = envelope.meta["person"]
    assert email in person["email_addresses"], person
