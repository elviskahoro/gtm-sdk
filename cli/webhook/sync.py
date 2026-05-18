"""Build a fresh registry by joining live Modal app list with Hookdeck wiring.

This is the meat of `gtm webhook sync`. The output schema is `Registry` from
cli/webhook/registry.py. All wire-protocol concerns (Modal CLI shell-out,
Hookdeck REST) are isolated in cli/webhook/_modal.py and cli/webhook/_hookdeck.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cli.webhook._hookdeck import fetch_inventory
from cli.webhook._modal import list_deployed_app_names, modal_url_for_app, warn
from cli.webhook.registry import (
    HandlerEntry,
    Registry,
    SingletonEntry,
    SourceEntry,
)
from libs.dlt.filesystem_gcp import CloudGoogle
from src.caldotcom.webhook.booking import Webhook as CaldotcomBookingWebhook
from src.fathom.webhook.call import Webhook as FathomCallWebhook
from src.fathom.webhook.message import Webhook as FathomMessageWebhook
from src.octolens.webhook import Webhook as OctolensMentionWebhook
from src.rb2b.webhook.visit import Webhook as Rb2bVisitWebhook

# (source slug used in registry, model class, display name for the registry).
# Display name is the alias used in webhooks/export_to_*.py imports — the
# class itself is named `Webhook` in every src/ file, so __name__ would
# collapse them all together.
SOURCES: list[tuple[str, type, str]] = [
    ("caldotcom", CaldotcomBookingWebhook, "CaldotcomBookingWebhook"),
    ("fathom-call", FathomCallWebhook, "FathomCallWebhook"),
    ("fathom-message", FathomMessageWebhook, "FathomMessageWebhook"),
    ("octolens", OctolensMentionWebhook, "OctolensMentionWebhook"),
    ("rb2b", Rb2bVisitWebhook, "Rb2bVisitWebhook"),
]

HANDLERS: list[str] = ["export_to_attio", "export_to_gcp_etl", "export_to_gcp_raw"]


def _app_name_for(handler: str, model: type) -> str | None:
    """Derive the Modal app name a given (model, handler) pair expects.

    Returns None if the model does not expose the method this handler needs —
    that's a coverage gap surfaced by the follow-up audit ticket
    (design/backlog-202605181000-is_valid_webhook_coverage_audit-ticket-01.md),
    not a sync failure.
    """
    if handler == "export_to_attio":
        if not hasattr(model, "attio_get_app_name"):
            return None
        return model.attio_get_app_name()

    if handler == "export_to_gcp_etl":
        if not hasattr(model, "etl_get_bucket_name"):
            return None
        return CloudGoogle.clean_bucket_name(bucket_name=model.etl_get_bucket_name())

    if handler == "export_to_gcp_raw":
        # No `raw_get_*` method exists on the models yet — derive the raw
        # bucket name from etl by swapping the stage suffix. The audit ticket
        # tracks adding a proper `raw_get_app_name()` method to each model.
        if not hasattr(model, "etl_get_bucket_name"):
            return None
        etl_bucket: str = model.etl_get_bucket_name()
        if not etl_bucket.endswith("-etl"):
            warn(
                f"{model.__name__}.etl_get_bucket_name()={etl_bucket!r} "
                "doesn't end in '-etl'; cannot derive raw bucket name. "
                "Skipping export_to_gcp_raw for this model.",
            )
            return None
        raw_bucket: str = etl_bucket[: -len("-etl")] + "-raw"
        return CloudGoogle.clean_bucket_name(bucket_name=raw_bucket)

    warn(f"unknown handler {handler!r}; ignoring")
    return None


def _build_handler_entry(
    handler: str,
    model: type,
    deployed_apps: set[str],
    hookdeck,
) -> HandlerEntry:
    app_name: str | None = _app_name_for(handler, model)
    if app_name is None:
        return HandlerEntry(
            handler=handler,
            deployed=False,
            modal_app=None,
            modal_url=None,
            hookdeck_source_id=None,
            hookdeck_destination_id=None,
            hookdeck_connection_id=None,
        )

    deployed: bool = app_name in deployed_apps
    modal_url: str | None = modal_url_for_app(app_name) if deployed else None

    src, dest, conn = (None, None, None)
    if modal_url is not None:
        src, dest, conn = hookdeck.find_by_modal_url(modal_url)

    return HandlerEntry(
        handler=handler,
        deployed=deployed,
        modal_app=app_name,
        modal_url=modal_url,
        hookdeck_source_id=src.id if src else None,
        hookdeck_destination_id=dest.id if dest else None,
        hookdeck_connection_id=conn.id if conn else None,
    )


def _build_test_bucket_singleton(
    deployed_apps: set[str],
    hookdeck,
) -> SingletonEntry:
    # webhooks/export_to_gcp_raw.py hardcodes this bucket name. It's a
    # source-agnostic dev passthrough — kept in the singletons section so the
    # registry stays an exhaustive inventory of webhook Modal apps.
    raw_bucket_name: str = "dlthub-devx-test-bucket"
    app_name: str = CloudGoogle.clean_bucket_name(bucket_name=raw_bucket_name)
    deployed: bool = app_name in deployed_apps
    modal_url: str | None = modal_url_for_app(app_name) if deployed else None
    src, dest, conn = (None, None, None)
    if modal_url is not None:
        src, dest, conn = hookdeck.find_by_modal_url(modal_url)
    return SingletonEntry(
        name="export_to_gcp_raw__test_bucket",
        deployed=deployed,
        modal_app=app_name,
        modal_url=modal_url,
        hookdeck_source_id=src.id if src else None,
        hookdeck_destination_id=dest.id if dest else None,
        hookdeck_connection_id=conn.id if conn else None,
    )


def build_registry() -> Registry:
    deployed_apps: set[str] = list_deployed_app_names()
    hookdeck = fetch_inventory()

    webhooks: list[SourceEntry] = []
    for source_slug, model, display_name in SOURCES:
        handlers: list[HandlerEntry] = [
            _build_handler_entry(h, model, deployed_apps, hookdeck) for h in HANDLERS
        ]
        webhooks.append(
            SourceEntry(
                source=source_slug,
                model=display_name,
                handlers=handlers,
            ),
        )

    singletons: list[SingletonEntry] = [
        _build_test_bucket_singleton(deployed_apps, hookdeck),
    ]

    return Registry(
        generated_at=datetime.now(UTC),
        webhooks=webhooks,
        singletons=singletons,
    )
