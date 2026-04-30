from __future__ import annotations

import os
from typing import Any


def get_client() -> Any:
    # Import here to avoid namespace collision with src/apollo
    import apollo as apollo_sdk

    api_key = os.environ.get("APOLLO_API_KEY")
    if api_key is None:
        raise ValueError(
            "APOLLO_API_KEY is not present in the environment.",
        )
    if api_key == "":
        raise ValueError(
            "APOLLO_API_KEY is present but empty.",
        )
    apollo_client_class = getattr(apollo_sdk, "ApolloSDK")
    return apollo_client_class(api_key=api_key)
