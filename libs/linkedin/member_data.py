from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# Removed Actor enum - using str instead to accept any person URN


class ClientExperience(BaseModel):
    clientGeneratedToken: UUID


class Content(BaseModel):
    format: str
    fallback: str
    formatVersion: int


class ContentClassification(BaseModel):
    classification: str


class Created(BaseModel):
    actor: str
    time: int


class Activity(BaseModel):
    actor: str | None = None
    URN: str | None = Field(None, alias="$URN")
    owner: str | None = None
    attachments: list[Any] | None = None
    clientExperience: ClientExperience | None = None
    author: str | None = None
    thread: str | None = None
    contentClassification: ContentClassification | None = None
    content: Content | None = None
    deliveredAt: int | None = None
    createdAt: int | None = None
    mailbox: str | None = None
    messageContexts: list[Any] | None = None
    id: str | None = None
    reactionType: str | None = None
    created: Created | None = None
    root: str | None = None
    lastModified: Created | None = None
    object: str | None = None


class ActivityStatus(Enum):
    SUCCESS = "SUCCESS"


class Method(Enum):
    CREATE = "CREATE"
    ACTION = "ACTION"
    DELETE = "DELETE"


class ResourceName(Enum):
    messages = "messages"
    social_actions_likes = "socialActions/likes"
    invitations = "invitations"
    social_actions_comments = "socialActions/comments"


class Element(BaseModel):
    owner: str
    resourceId: str
    method: Method
    activity: Activity
    configVersion: int
    parentSiblingActivities: list[Any]
    resourceName: ResourceName
    resourceUri: str
    actor: str
    activityId: UUID
    processedAt: int
    activityStatus: ActivityStatus
    capturedAt: int
    siblingActivities: list[Any]
    id: int


class Link(BaseModel):
    type: str
    rel: str
    href: str


class Paging(BaseModel):
    start: int
    count: int
    links: list[Link]
    total: int


class Empty(BaseModel):
    paging: Paging
    elements: list[Element]
