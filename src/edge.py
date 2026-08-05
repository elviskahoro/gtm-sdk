# trunk-ignore-all(ruff/ANN401,ruff/EM102,ruff/F822,ruff/TRY003,pyright/reportUnsupportedDunderAll): dynamic facade exports are resolved from eager adapter package roots.
"""Stable edge-facing facade for CLI, scripts, and webhook adapter access.

The facade centralizes edge-to-adapter dependencies in the closed
orchestration graph and keeps callers independent of adapter module layout.
"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Keep Tach aware of the Gmail adapter used by the dynamic facade. The
    # runtime lookup remains lazy through ``_MODULES``.
    from libs.gmail import decode_token, extract_id_from_url

_MODULES: tuple[str, ...] = (
    "libs.attio",
    "libs.caldotcom",
    "libs.clay_http",
    "libs.dlt",
    "libs.exa",
    "libs.fathom",
    "libs.fireflies",
    "libs.filesystem",
    "libs.granola",
    "libs.gmail",
    "libs.harvest",
    "libs.infisical",
    "libs.linear",
    "libs.logging.structured",
    "libs.motherduck",
    "libs.octolens",
    "libs.parallel",
    "libs.parsers",
    "libs.rb2b",
    "libs.sanity",
    "libs.slack",
    "libs.telemetry",
    "libs.webhook",
)

_EXTRA_ALIASES: dict[str, tuple[str, str]] = {
    "CloudGoogle": ("libs.dlt", "CloudGoogle"),
    "GCPCredentials": ("libs.dlt", "GCPCredentials"),
    "linear_client": ("libs.linear", "client"),
    "sanity_api_key_scope": ("libs.sanity", "api_key_scope"),
    # Both Exa and Parallel expose a `search` helper; the stub and existing
    # facade contract identify this name as Parallel's search API.
    "search": ("libs.parallel", "search"),
    "Rb2bWebhook": ("libs.rb2b", "Webhook"),
    "slack_get_client": ("libs.slack", "get_client"),
}

_infisical = import_module("libs.infisical")
infisical = SimpleNamespace(
    fetch=_infisical.fetch,
    fetch_all=_infisical.fetch_all,
)


def fetch_token_scopes(*args: Any, **kwargs: Any) -> Any:
    """Delegate lazily so existing preflight monkeypatches remain effective."""
    attio = import_module("libs.attio")
    return attio.preflight.fetch_token_scopes(*args, **kwargs)


def __getattr__(name: str) -> object:
    """Resolve adapter-owned symbols while preserving stable ``src.edge`` imports.

    The facade keeps callers independent of adapter module layout while the
    adapter package roots remain the owners of their exported symbols.
    """
    if name in _EXTRA_ALIASES:
        module_name, attribute = _EXTRA_ALIASES[name]
        return getattr(import_module(module_name), attribute)
    for module_name in _MODULES:
        module = import_module(module_name)
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))


__all__ = [
    "DEFAULT_API_VERSION",
    "DEFAULT_DATASET",
    "DEFAULT_PROJECT_ID",
    "UNSUPPORTED_SLACK_CHANNEL_SECRET",
    "AttioConflictError",
    "AttioError",
    "AttioValidationError",
    "AttributeCreateResult",
    "BookingAttendee",
    "BookingCreatedPayload",
    "BookingHost",
    "CalcomClient",
    "CloudGoogle",
    "CompanyInput",
    "CompanyResult",
    "CompanySearchResult",
    "ConfigurationError",
    "ConnectivityError",
    "DeploymentMismatchError",
    "DestinationFileData",
    "DestinationType",
    "DuplicateSlugError",
    "ErrorEntry",
    "EventType",
    "ExportCliJsonPayload",
    "ExportRunOptions",
    "FileUtility",
    "GCPCredentials",
    "GranolaError",
    "InfisicalAuthError",
    "InfisicalFetchError",
    "MatchCondition",
    "MeetingCandidate",
    "MeetingExternalRef",
    "MeetingInput",
    "MeetingLinkedRecord",
    "MeetingParticipantInput",
    "MentionInput",
    "MutationAttendee",
    "NoShowAttendee",
    "NoteInput",
    "NoteResult",
    "OctolensClient",
    "Payload",
    "PersonInput",
    "Rb2bWebhook",
    "ReliabilityEnvelope",
    "SanityConfig",
    "SchemaMismatchError",
    "Source",
    "SourceFileData",
    "TrackingEventInput",
    "UnsafeArchiveDirError",
    "WarningEntry",
    "Webhook",
    "WebhookFilter",
    "WebhookFilters",
    "assert_attio_token_scopes",
    "build_patch_record_request",
    "compute_event_id",
    "connect",
    "create_attribute",
    "create_companies_attribute",
    "create_object",
    "decode_token",
    "describe_attio_error",
    "emit_cli_event",
    "ensure_select_options",
    "error_envelope",
    "etl_bucket_name",
    "extract_id_from_url",
    "fetch",
    "fetch_all",
    "fetch_profile",
    "fetch_token_scopes",
    "find_companies",
    "find_or_create_meeting",
    "find_people",
    "findall_create",
    "findall_result",
    "findall_status",
    "from_motherduck_row",
    "get_client",
    "get_company_values",
    "get_person_values",
    "infisical",
    "init_log_exporter",
    "init_tracer",
    "is_unknown_filter_attribute",
    "iter_meetings",
    "iter_meetings_in_range",
    "linear_client",
    "list_attributes",
    "list_candidate_meetings",
    "list_select_options",
    "list_status_options",
    "log",
    "lookup_user_id_by_email",
    "model_dump_or_empty",
    "normalize_linkedin_url",
    "normalize_mapping_payload",
    "normalize_rb2b_timestamp",
    "post_message",
    "post_row",
    "query",
    "raw_bucket_name",
    "resolve_owner_member_id",
    "resolve_record_id_for_ref",
    "sanity_api_key_scope",
    "search",
    "search_companies",
    "search_people",
    "set_company_domain_if_empty",
    "set_source",
    "slack_get_client",
    "span",
    "update_person",
    "upsert_company",
    "upsert_person",
    "webhook_from_sdk_meeting",
    "webhook_request_context",
    "write_meeting_export",
]
