from __future__ import annotations

from libs.resend.email import SendEmailInput


def test_send_email_input_defaults():
    inp = SendEmailInput(
        to=["test@example.com"],
        subject="Test",
        html="<b>hi</b>",
    )
    assert inp.sender == "mail@elvis.ai"
    assert len(inp.to) == 1
