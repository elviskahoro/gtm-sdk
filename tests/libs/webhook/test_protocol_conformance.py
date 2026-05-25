"""Verify each source's ``Webhook`` class satisfies ``WebhookModelProtocol``.

Catches missing contract methods at ``pytest`` time instead of at
``modal deploy`` time when the placeholder substitution lands in an image
build. Add a new source's import here before wiring it into the
``webhooks/`` handlers.
"""

from __future__ import annotations

import pytest

from libs.webhook.protocol import WebhookModelProtocol
from src.caldotcom.webhook.booking import Webhook as CaldotcomBookingWebhook
from src.fathom.webhook.call import Webhook as FathomCallWebhook
from src.fathom.webhook.message import Webhook as FathomMessageWebhook
from src.octolens.webhook.mention import Webhook as OctolensMentionWebhook
from src.rb2b.webhook.visit import Webhook as Rb2bVisitWebhook

# Pydantic 2's `@runtime_checkable` Protocol check on a *class* is sufficient
# here — every contract member is either a staticmethod or an instance method
# accessible via the class, so issubclass-style structural conformance is
# adequate without constructing a real instance (which would require valid
# payloads for each source). The Protocol's `__subclasshook__` walks the
# method names; a missing method on a source class fails this test before
# any handler ever imports it.
SOURCES = [
    pytest.param(CaldotcomBookingWebhook, id="caldotcom-booking"),
    pytest.param(FathomCallWebhook, id="fathom-call"),
    pytest.param(FathomMessageWebhook, id="fathom-message"),
    pytest.param(OctolensMentionWebhook, id="octolens-mention"),
    pytest.param(Rb2bVisitWebhook, id="rb2b-visit"),
]


@pytest.mark.parametrize("webhook_cls", SOURCES)
def test_webhook_satisfies_protocol(webhook_cls: type) -> None:
    assert issubclass(webhook_cls, WebhookModelProtocol), (
        f"{webhook_cls.__module__}.{webhook_cls.__qualname__} is missing one "
        f"or more methods required by WebhookModelProtocol. Compare its "
        f"surface against libs/webhook/protocol.py."
    )
