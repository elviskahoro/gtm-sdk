from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from pydantic.dataclasses import dataclass

from .model_chat_response import ChatResponse

if TYPE_CHECKING:
    from logging import Logger

    from httpx import Response

    from .model_chat_completion import ChatCompletion


BASE_API_URL: str = "https://api.perplexity.ai"
TIMEOUT: int = 30
SPAN_KEY: str = "perplexity"


@dataclass
class Client:
    token: str

    @classmethod
    def set_up_client_from_tokens(
        cls: type[Client],
        token: str | None,
        logger: Logger,
    ) -> Client | None:
        if token is None:
            error_msg: str = "Perplexity token is required."
            logger.error(error_msg)
            raise AssertionError(error_msg)

        return cls(
            token=token,
        )

    def chat_completion(
        self: Client,
        chat_completion: ChatCompletion,
        logger: Logger,
    ) -> ChatResponse:
        api_key = self.token
        headers: dict[str, str] = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(
                timeout=TIMEOUT,
            ) as client:
                response: Response = client.post(
                    url=f"{BASE_API_URL}/chat/completions",
                    headers=headers,
                    content=chat_completion.model_dump_json(),
                )
                response.raise_for_status()

        except httpx.ReadTimeout:
            logger.exception("Perplexity API request timed out")
            raise

        return ChatResponse.model_validate(
            obj=response.json(),
        )
