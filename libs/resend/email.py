import os
from typing import Any

from pydantic import BaseModel


class SendEmailInput(BaseModel):
    sender: str = "mail@elvis.ai"
    to: list[str]
    subject: str
    html: str


def _resend_module() -> Any:
    import resend as resend_sdk

    return resend_sdk


def send_test_email(api_key: str, input: SendEmailInput | None = None) -> None:
    if input is None:
        input = SendEmailInput(
            to=["ekk0809@gmail.com"],
            subject="Hello from Resend",
            html="<strong>It works!</strong>",
        )
    resend = _resend_module()
    setattr(resend, "api_key", api_key)

    params: dict[str, object] = {
        "from": input.sender,
        "to": input.to,
        "subject": input.subject,
        "html": input.html,
    }

    try:
        emails_client = getattr(resend, "Emails")
        email = emails_client.send(params)
        print(f"Email sent successfully! ID: {email.get('id')}")
        print(email)

    except Exception as e:
        print(f"Failed to send email: {e}")


if __name__ == "__main__":
    # Load API key from environment variable
    api_key = os.environ.get("RESEND_API_KEY")

    if not api_key:
        print("Error: RESEND_API_KEY environment variable not set.")
        print("Please set it using: export RESEND_API_KEY='re_...'")
    else:
        send_test_email(api_key)
