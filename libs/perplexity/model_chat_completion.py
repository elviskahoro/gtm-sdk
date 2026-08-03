# trunk-ignore-all(pyrefly/bad-index)
from __future__ import annotations

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str = "user"
    content: str


# noinspection PyDataclass
class ChatCompletion(BaseModel):
    model: str | None = "llama-3.1-sonar-small-128k-online"
    messages: list[ChatMessage] | None
    max_tokens: str | None = None
    temperature: float | None = 0.2
    top_p: float | None = 0.9
    return_citations: bool | None = True
    search_domain_filter: list[str] | None = ["perplexity.ai"]
    return_images: bool | None = False
    return_related_questions: bool | None = False
    search_recency_filter: str | None = "month"
    top_k: int | None = 0
    stream: bool | None = False
    presence_penalty: int | None = 0
    frequency_penalty: int | None = 1
