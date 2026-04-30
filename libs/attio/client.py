import os

from libs.attio.errors import AttioAuthError
from libs.attio.sdk_boundary import get_attio_sdk_client_class


def get_client():
    token: str = os.environ.get("ATTIO_API_KEY", "").strip()
    if not token:
        raise AttioAuthError(
            "ATTIO_API_KEY environment variable is required but not set.",
        )
    Attio = get_attio_sdk_client_class()
    return Attio(oauth2=token)
