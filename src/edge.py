# trunk-ignore-all(ruff/ANN401,ruff/EM102,ruff/F822,ruff/TRY003,pyright/reportUnsupportedDunderAll): dynamic facade exports are resolved from eager adapter package roots.
"""Stable edge-facing facade for CLI, scripts, and webhook adapter access.

The facade centralizes edge-to-adapter dependencies in the closed
orchestration graph and keeps callers independent of adapter module layout.
"""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

from libs import (
    attio,
    caldotcom,
    clay_http,
    dlt,
    exa,
    fathom,
    fireflies,
    gmail,
    granola,
    harvest,
    infisical as _infisical,
    linear,
    logging,
    motherduck,
    octolens,
    parallel,
    parsers,
    rb2b,
    sanity,
    slack,
    telemetry,
    webhook,
)
from libs.filesystem import files as filesystem_files
from libs.sanity import api_key_scope as sanity_api_key_scope

_MODULES: tuple[ModuleType, ...] = (
    attio,
    caldotcom,
    clay_http,
    dlt,
    exa,
    fathom,
    fireflies,
    filesystem_files,
    granola,
    gmail,
    harvest,
    _infisical,
    linear,
    logging.structured,
    motherduck,
    octolens,
    parallel,
    parsers,
    rb2b,
    sanity,
    slack,
    telemetry,
    webhook,
)

_EXTRA_ALIASES: dict[str, Any] = {
    "linear_client": linear.client,
    # Both Exa and Parallel expose a `search` helper; the stub and existing
    # facade contract identify this name as Parallel's search API.
    "search": parallel.search,
    "Rb2bWebhook": rb2b.Webhook,
    "slack_get_client": slack.get_client,
}

infisical = SimpleNamespace(
    fetch=_infisical.fetch,
    fetch_all=_infisical.fetch_all,
)


def fetch_token_scopes(*args: Any, **kwargs: Any) -> Any:
    """Delegate lazily so existing preflight monkeypatches remain effective."""
    return attio.preflight.fetch_token_scopes(*args, **kwargs)


def __getattr__(name: str) -> Any:
    """Resolve a facade symbol from its owning adapter on first access."""
    if name in _EXTRA_ALIASES:
        return _EXTRA_ALIASES[name]
    for module in _MODULES:
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
    "WebhookModelTypeCheckShim",
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
