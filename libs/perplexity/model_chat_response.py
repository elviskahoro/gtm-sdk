from __future__ import annotations

from pydantic import BaseModel


class Delta(BaseModel):
    role: str | None = None
    content: str | None = None


class Choice(BaseModel):
    index: int | None = None
    finish_reason: str | None = None
    message: Delta | None = None
    delta: Delta | None = None


class Usage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ChatResponse(BaseModel):
    id: str | None = None
    model: str | None = None
    object: str | None = None
    created: int | None = None
    choices: list[Choice] | None = None
    usage: Usage | None = None

    # noinspection PyUnboundLocalVariable
    def get_content(
        self: ChatResponse,
    ) -> str | None:
        if (
            self.choices is not None
            and (first_choice := self.choices[0])
            and first_choice.message is not None
        ):
            return first_choice.message.content

        return None
