# trunk-ignore-all(ruff/TC004): the shim is a type-check-only deploy placeholder.
from typing import TYPE_CHECKING

from .filter import WebhookFilter, WebhookFilters
from .protocol import (
    UNSUPPORTED_SLACK_CHANNEL_SECRET,
    WebhookModelProtocol,
)

if TYPE_CHECKING:
    from .protocol import WebhookModelTypeCheckShim

__all__ = [
    "UNSUPPORTED_SLACK_CHANNEL_SECRET",
    "WebhookFilter",
    "WebhookFilters",
    "WebhookModelProtocol",
    "WebhookModelTypeCheckShim",
]
