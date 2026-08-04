"""Attio CRM adapter — typed wrapper around the Attio REST API and Python SDK.

``__all__`` below is this package's **public API declaration**, not a
convenience re-export. Downstream repos install gtm-sdk as a dependency, so a
symbol can be unreferenced everywhere inside this repo and still be
load-bearing; naming it here is what distinguishes supported surface from an
internal helper. Removing an entry is a breaking change.

Companion mechanism: ``contracts/downstream_api.toml`` records which symbols
known consumers actually import (including the handful of private helpers they
reach into, which deliberately stay out of ``__all__``), and
``tests/test_downstream_contract.py`` fails the build if any of them disappear.
See AGENTS.md, "Public API and downstream consumers".
"""

from __future__ import annotations

from .attributes import (
    create_attribute,
    create_companies_attribute,
    ensure_select_options,
    is_select_attribute_writable,
    list_attributes,
    list_select_options,
    list_status_options,
)
from .client import api_key_scope, get_client, resolve_api_key
from .companies import (
    add_company,
    find_company_by_domain,
    find_company_by_name,
    get_company_values,
    search_companies,
    set_company_domain_if_empty,
    set_company_owner,
    stub_create_company,
    update_company,
    upsert_company,
)
from .contracts import ErrorEntry, ReliabilityEnvelope, SkippedField, WarningEntry
from .errors import (
    AttioConflictError,
    AttioError,
    AttioValidationError,
    ConfigurationError,
    ConnectivityError,
    DeploymentMismatchError,
    SchemaMismatchError,
)
from .ext_tam import (
    EXT_TAM_OBJECT,
    find_by_person_and_account,
    iter_company_ids_by_filter,
    upsert_ext_tam,
)
from .gtm_content import GTM_CONTENT_OBJECT, find_by_slug, upsert_gtm_content
from .models import (
    AttributeCreateResult,
    AttributeInfo,
    CompanyInput,
    CompanyResult,
    CompanySearchResult,
    ExtTamInput,
    GtmContentInput,
    NoteInput,
    NoteResult,
    ObjectCreateResult,
    PersonInput,
    PersonResult,
    PersonSearchResult,
)
from .models import (
    MeetingCandidate,
    MeetingExternalRef,
    MeetingInput,
    MeetingLifecycleEventInput,
    MeetingLinkedRecord,
    MeetingParticipantInput,
    MentionInput,
    TrackingEventInput,
)
from .meetings import find_or_create_meeting, iter_meetings_in_range, list_candidate_meetings
from .mentions import upsert_mention
from .notes import (
    add_note,
    create_note,
    find_note_by_title,
    list_notes_for_parent,
    resolve_record_id_for_ref,
    update_note,
)
from .objects import create_object, list_object_api_slugs
from .people import (
    add_person,
    find_person_by_name_at_company,
    find_person_by_sanity_author_id,
    get_person_values,
    search_people,
    set_person_sanity_author_id,
    stub_create_person,
    update_person,
    upsert_person,
    error_envelope,
)
from .preflight import assert_attio_token_scopes, fetch_token_scopes, resolve_owner_member_id
from .sdk_boundary import (
    AttioErrorDescription,
    build_assert_record_request,
    build_patch_record_request,
    build_post_record_request,
    describe_attio_error,
    is_unknown_filter_attribute,
    is_uniqueness_conflict,
    model_dump_or_empty,
)
from .values import (
    format_company_description,
    format_company_domains,
    format_company_linkedin,
    format_location_from_parts,
    normalize_company_name,
    normalize_linkedin_company_url,
    normalize_linkedin_url,
    looks_like_domain,
)
from .tracking_events import find_or_create_meeting_lifecycle_event, find_or_create_tracking_event

__all__ = [
    "EXT_TAM_OBJECT",
    "GTM_CONTENT_OBJECT",
    "AttioErrorDescription",
    "AttioConflictError",
    "AttioError",
    "AttioValidationError",
    "AttributeCreateResult",
    "AttributeInfo",
    "CompanyInput",
    "CompanyResult",
    "CompanySearchResult",
    "ConfigurationError",
    "ConnectivityError",
    "DeploymentMismatchError",
    "ErrorEntry",
    "ExtTamInput",
    "GtmContentInput",
    "MeetingCandidate",
    "MeetingExternalRef",
    "MeetingInput",
    "MeetingLifecycleEventInput",
    "MeetingLinkedRecord",
    "MeetingParticipantInput",
    "MentionInput",
    "NoteInput",
    "NoteResult",
    "ObjectCreateResult",
    "PersonInput",
    "PersonResult",
    "PersonSearchResult",
    "ReliabilityEnvelope",
    "SchemaMismatchError",
    "SkippedField",
    "TrackingEventInput",
    "WarningEntry",
    "add_company",
    "add_note",
    "add_person",
    "assert_attio_token_scopes",
    "api_key_scope",
    "build_assert_record_request",
    "build_patch_record_request",
    "build_post_record_request",
    "create_attribute",
    "create_companies_attribute",
    "create_note",
    "create_object",
    "describe_attio_error",
    "ensure_select_options",
    "error_envelope",
    "fetch_token_scopes",
    "find_by_person_and_account",
    "find_by_slug",
    "find_company_by_domain",
    "find_company_by_name",
    "find_note_by_title",
    "find_person_by_name_at_company",
    "find_person_by_sanity_author_id",
    "format_company_description",
    "format_company_domains",
    "format_company_linkedin",
    "format_location_from_parts",
    "find_or_create_meeting",
    "find_or_create_meeting_lifecycle_event",
    "find_or_create_tracking_event",
    "get_client",
    "get_company_values",
    "get_person_values",
    "is_select_attribute_writable",
    "is_uniqueness_conflict",
    "is_unknown_filter_attribute",
    "iter_company_ids_by_filter",
    "iter_meetings_in_range",
    "list_attributes",
    "list_notes_for_parent",
    "list_object_api_slugs",
    "list_select_options",
    "list_status_options",
    "list_candidate_meetings",
    "looks_like_domain",
    "model_dump_or_empty",
    "normalize_company_name",
    "normalize_linkedin_company_url",
    "normalize_linkedin_url",
    "resolve_api_key",
    "resolve_record_id_for_ref",
    "search_companies",
    "search_people",
    "set_company_domain_if_empty",
    "set_company_owner",
    "set_person_sanity_author_id",
    "stub_create_company",
    "stub_create_person",
    "update_company",
    "update_note",
    "update_person",
    "upsert_company",
    "upsert_ext_tam",
    "upsert_gtm_content",
    "upsert_person",
    "upsert_mention",
    "resolve_owner_member_id",
]
