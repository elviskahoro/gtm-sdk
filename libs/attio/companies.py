import logging
from typing import Any

from libs.attio.client import get_client
from libs.attio.contracts import (
    ErrorEntry,
    ReliabilityEnvelope,
    WarningEntry,
)
from libs.attio.errors import (
    AttioConflictError,
    AttioNotFoundError,
    AttioValidationError,
    classify_error,
)
from libs.attio.models import CompanyInput, CompanyResult, CompanySearchResult
from libs.attio.sdk_boundary import (
    build_patch_record_request,
    build_post_record_request,
    extract_existing_record_id,
    is_uniqueness_conflict,
    model_dump_or_empty,
)
from libs.attio.values import (
    format_company_description,
    format_company_domains,
    format_company_linkedin,
    format_company_name,
    normalize_company_name,
)


def _build_values(input: CompanyInput, partial: bool = False) -> dict[str, Any]:
    values: dict[str, Any] = {}

    if input.name:
        values["name"] = format_company_name(input.name)

    # Attio's ``domains`` attribute applies TLD validation and rejects RFC-2606
    # reserved TLDs (``.test``, ``.invalid``, ``.example``, ``.localhost``)
    # with the *misleading* error
    # ``An invalid value was passed to attribute with slug "domains"``.
    # The error names the attribute, not the value, so it reads like a schema
    # problem. It isn't — the writer below is shape-correct; the offending
    # input is a value with a reserved TLD. Commit ``2763b67`` misdiagnosed
    # this and disabled the writer entirely; ai-21r restored it after
    # ``tmp/probe_company_domain_write.py`` confirmed real domains write fine.
    # Use ``example.com`` (also RFC-reserved but accepted by Attio) for any
    # probe/fixture domains — same convention as ``format_email_addresses_for_write``.
    domains = format_company_domains(input.domain)
    if domains:
        values["domains"] = domains

    description = format_company_description(input.description)
    if description:
        values["description"] = description

    # The Attio standard ``companies`` object exposes a writable ``linkedin``
    # slug (type=text, single-value) — confirmed via
    # ``tmp/probe_company_linkedin_write.py``. ``format_company_linkedin``
    # normalizes through ``/company/<slug>`` shape, so profile URLs slipped
    # into ``input.linkedin_url`` will canonicalize to None and be dropped
    # instead of polluting the Company record.
    linkedin = format_company_linkedin(input.linkedin_url)
    if linkedin:
        values["linkedin"] = linkedin

    return values


def _extract_result(data: Any, created: bool) -> CompanyResult:
    raw: dict[str, Any] = model_dump_or_empty(data)
    record_id: str = data.id.record_id

    name: str | None = None
    domains: list[str] = []

    name_values = data.values.get("name", [])
    for nv in name_values:
        if hasattr(nv, "value"):
            name = nv.value
            break

    domain_values = data.values.get("domains", [])
    for dv in domain_values:
        if hasattr(dv, "domain"):
            domains.append(dv.domain)

    return CompanyResult(
        record_id=record_id,
        name=name,
        domains=domains,
        created=created,
        raw=raw,
    )


def _extract_search_result(data: Any) -> CompanySearchResult:
    record_id: str = data.id.record_id

    name: str | None = None
    for nv in data.values.get("name", []):
        if hasattr(nv, "value"):
            name = nv.value
            break

    domains: list[str] = []
    for dv in data.values.get("domains", []):
        if hasattr(dv, "domain"):
            domains.append(dv.domain)

    description: str | None = None
    for desc in data.values.get("description", []):
        if hasattr(desc, "value"):
            description = desc.value
            break

    return CompanySearchResult(
        record_id=record_id,
        name=name,
        domains=domains,
        description=description,
    )


def search_companies(
    name: str | None = None,
    domain: str | None = None,
    limit: int = 25,
) -> list[CompanySearchResult]:
    conditions: list[dict[str, Any]] = []
    if name:
        conditions.append({"name": {"$contains": name}})
    if domain:
        conditions.append({"domains": domain})

    if not conditions:
        raise AttioValidationError("Provide at least one search criterion.")

    if len(conditions) == 1:
        filter_: dict[str, Any] = conditions[0]
    else:
        filter_ = {"$and": conditions}

    with get_client() as client:
        response = client.records.post_v2_objects_object_records_query(
            object="companies",
            filter_=filter_,
            limit=limit,
        )
        return [_extract_search_result(record) for record in response.data]


def add_company(input: CompanyInput) -> CompanyResult:
    values = _build_values(input)
    with get_client() as client:
        try:
            response = client.records.post_v2_objects_object_records(
                object="companies",
                data=build_post_record_request(values),
            )

        except Exception as e:
            if is_uniqueness_conflict(e):
                existing_id = extract_existing_record_id(e)
                # `from None` suppresses the SDK's ResponseValidationError chain — its
                # __cause__ is a pydantic ValidationError firing because the SDK's Code
                # Literal omits "uniqueness_conflict". The conflict itself is expected,
                # so we don't want Modal logging the pydantic traceback for every dupe.
                raise AttioConflictError(
                    "Company already exists. Use 'update' instead."
                    + (f" Existing record ID: {existing_id}" if existing_id else ""),
                    existing_record_id=existing_id,
                ) from None
            raise

        return _extract_result(response.data, created=True)


def _result_envelope(
    *,
    action: str,
    result: CompanyResult,
    warnings: list[WarningEntry] | None = None,
    partial_success: bool = False,
) -> ReliabilityEnvelope:
    return ReliabilityEnvelope(
        success=True,
        partial_success=partial_success,
        action=action,  # type: ignore[arg-type]
        record_id=result.record_id,
        warnings=warnings or [],
        skipped_fields=[],
        errors=[],
        meta={"output_schema_version": "v1", "company": result.model_dump()},
    )


def upsert_company(input: CompanyInput) -> ReliabilityEnvelope:
    """Search by domain (preferred) or name, then add or update.

    Mirrors ``libs.attio.people.upsert_person``. Domain is the stronger
    deduplication key — name spellings drift (``"Acme"`` vs ``"Acme, Inc"``
    vs ``"acme"``) while ``domains`` is canonical. When ``input.domain`` is
    set we search by domain; otherwise we fall back to name. When neither
    is set we fall through to creating a new company.

    Multi-match picks the lexicographically smallest ``record_id`` and flags
    the envelope as ``partial_success`` with a
    ``upsert_multi_match_selected_record`` warning.
    """
    matches: list[CompanySearchResult] = []
    if input.domain:
        matches = search_companies(domain=input.domain, limit=50)
    elif input.name:
        matches = search_companies(name=input.name, limit=50)

    if len(matches) == 0:
        result = add_company(input)
        return _result_envelope(action="created", result=result)

    if len(matches) == 1:
        result = update_company(
            record_id=matches[0].record_id,
            domain=None,
            input=input,
        )
        return _result_envelope(action="updated", result=result)

    selected = sorted(m.record_id for m in matches)[0]
    result = update_company(record_id=selected, domain=None, input=input)
    warnings = [
        WarningEntry(
            code="upsert_multi_match_selected_record",
            message=(
                "Multiple companies matched; selected lexicographically "
                "smallest record_id."
            ),
            field="record_id",
            retryable=False,
        ),
    ]
    return _result_envelope(
        action="updated",
        result=result,
        warnings=warnings,
        partial_success=True,
    )


def error_envelope(error: Exception, *, strict: bool = False) -> ReliabilityEnvelope:
    classified = classify_error(error, strict=strict)
    return ReliabilityEnvelope(
        success=False,
        partial_success=False,
        action="failed",
        record_id=None,
        warnings=[],
        skipped_fields=[],
        errors=[
            ErrorEntry(
                code=classified.code,
                message=classified.message,
                error_type=classified.error_type,
                fatal=classified.fatal,
                field=classified.field,
            ),
        ],
        meta={"output_schema_version": "v1"},
    )


def get_company_values(domain: str) -> dict[str, Any] | None:
    """Look up a company by domain; return their field values dict or None."""
    try:
        with get_client() as client:
            response = client.records.post_v2_objects_object_records_query(
                object="companies",
                filter_={"domains": domain},
                limit=1,
            )

            if not response.data:
                return None

            record = response.data[0]
            return dict(record.values or {})
    except Exception:
        # If lookup fails, return None to allow the upsert to proceed
        return None


def update_company(
    record_id: str | None,
    domain: str | None,
    input: CompanyInput,
) -> CompanyResult:
    with get_client() as client:
        if not record_id:
            if not domain:
                raise AttioNotFoundError(
                    "Provide --id or --domain to identify the company.",
                )
            query_response = client.records.post_v2_objects_object_records_query(
                object="companies",
                filter_={"domains": domain},
                limit=1,
            )
            if not query_response.data:
                raise AttioNotFoundError(
                    f"No company found with domain: {domain}",
                )
            record_id = query_response.data[0].id.record_id

        values = _build_values(input, partial=True)
        try:
            response = client.records.patch_v2_objects_object_records_record_id_(
                object="companies",
                record_id=record_id,
                data=build_patch_record_request(values),
            )
        except Exception as e:
            # PATCH can hit a uniqueness conflict when a unique attribute
            # (e.g. domain) is updated to a value already held by another
            # record. Suppress the SDK pydantic chain — see add_company.
            if is_uniqueness_conflict(e):
                existing_id = extract_existing_record_id(e)
                raise AttioConflictError(
                    "Company update conflicts with an existing record."
                    + (f" Existing record ID: {existing_id}" if existing_id else ""),
                    existing_record_id=existing_id,
                ) from None
            raise

        return _extract_result(response.data, created=False)


logger = logging.getLogger(__name__)


def find_company_by_domain(domain: str) -> str | None:
    """Find a Company record by exact domain match. Returns lex-smallest on multi-match."""
    if not domain or not domain.strip():
        return None
    with get_client() as client:
        response = client.records.post_v2_objects_object_records_query(
            object="companies",
            filter_={"domains": {"domain": domain.strip().lower()}},
            limit=10,
        )
        if not response.data:
            return None
        return sorted(r.id.record_id for r in response.data)[0]


def find_company_by_name(name: str) -> str | None:
    """Find a Company record by exact name first, then normalized fallback.

    Exact match: server-side ``name = {"$eq": name}`` filter.
    Normalized fallback: ``$contains`` query for the first significant
    word of the name, then client-side filter via ``normalize_company_name``.
    Returns the lexicographically smallest matching record_id on multi-match.
    """
    if not name or not name.strip():
        return None

    with get_client() as client:
        # 1. Try exact match.
        exact_response = client.records.post_v2_objects_object_records_query(
            object="companies",
            filter_={"name": {"$eq": name.strip()}},
            limit=2,
        )
        if exact_response.data:
            return sorted(r.id.record_id for r in exact_response.data)[0]

        # 2. Normalized fallback: $contains on the first significant token.
        target = normalize_company_name(name)
        if not target:
            return None
        # Use the un-normalized first token for the server filter; we
        # post-filter client-side via normalize_company_name.
        first_token = target.split(" ", 1)[0]
        broad_response = client.records.post_v2_objects_object_records_query(
            object="companies",
            filter_={"name": {"$contains": first_token}},
            limit=50,
        )
        candidates: list[str] = []
        for rec in broad_response.data:
            for nv in rec.values.get("name", []):
                value = getattr(nv, "value", None)
                if value and normalize_company_name(value) == target:
                    candidates.append(rec.id.record_id)
                    break
        if not candidates:
            return None
        return sorted(candidates)[0]


def stub_create_company(name: str, *, apply: bool) -> str:
    """Create a Company with only ``name`` set. Idempotent via 409 handling.

    Preview mode returns a synthetic ``preview-<n>`` id so downstream code
    can keep wiring values without writing. Apply mode reuses the existing
    ``add_company`` path and falls back to ``extract_existing_record_id``
    on uniqueness conflict.
    """
    if not apply:
        return f"preview-{name}"

    try:
        result = add_company(CompanyInput(name=name))
        return result.record_id
    except AttioConflictError as exc:
        existing = getattr(exc, "existing_record_id", None)
        if existing:
            return existing
        # No existing_record_id in the conflict payload — fall back to a
        # name lookup.
        found = find_company_by_name(name)
        if found:
            return found
        raise


_OWNER_ATTR_SLUG_CACHE: dict[str, str] = {}


def _resolve_owner_attr_slug() -> str:
    """Resolve and cache the Companies ``Owner`` attribute slug.

    Attio's "Owner" attribute is an actor-reference; the slug may be
    ``owner`` (default) but workspaces can rename it. Looking up the slug
    once via metadata avoids hard-coding.
    """
    if "companies" in _OWNER_ATTR_SLUG_CACHE:
        return _OWNER_ATTR_SLUG_CACHE["companies"]
    with get_client() as client:
        response = client.attributes.get_v2_target_identifier_attributes(
            target="objects",
            identifier="companies",
        )
        for attr in response.data:
            title = (getattr(attr, "title", "") or "").lower()
            if title == "owner":
                slug = getattr(attr, "api_slug", "owner") or "owner"
                _OWNER_ATTR_SLUG_CACHE["companies"] = slug
                return slug
    # Default to "owner" if metadata didn't resolve.
    _OWNER_ATTR_SLUG_CACHE["companies"] = "owner"
    return "owner"


def _extract_current_owner_id(record_values: Any, slug: str) -> str | None:
    if isinstance(record_values, dict):
        owner_values = record_values.get(slug, [])
    else:
        owner_values = getattr(record_values, slug, []) or []
    for ov in owner_values:
        # Attio actor-reference attribute exposes ``referenced_actor_id`` on
        # the value; older shapes may use ``target_record_id``.
        for attr_name in ("referenced_actor_id", "target_record_id"):
            value = getattr(ov, attr_name, None)
            if value:
                return value
    return None


def set_company_owner(
    *,
    company_record_id: str,
    person_record_id: str,
    apply: bool,
) -> ReliabilityEnvelope:
    """Set ``Company.Owner`` on a Companies record.

    Reads the current value first and PATCHes only when the requested
    person differs. Preview mode is a pure noop. The Owner attribute slug
    is resolved once per process via ``_resolve_owner_attr_slug``.
    """
    if not apply:
        return ReliabilityEnvelope(
            success=True,
            partial_success=False,
            action="noop",
            record_id=company_record_id,
            warnings=[],
            skipped_fields=[],
            errors=[],
            meta={"output_schema_version": "v1", "preview": True},
        )

    slug = _resolve_owner_attr_slug()
    with get_client() as client:
        current = client.records.get_v2_objects_object_records_record_id_(
            object="companies",
            record_id=company_record_id,
        )
        existing = _extract_current_owner_id(current.data.values, slug)
        if existing == person_record_id:
            return ReliabilityEnvelope(
                success=True,
                partial_success=False,
                action="noop",
                record_id=company_record_id,
                warnings=[],
                skipped_fields=[],
                errors=[],
                meta={"output_schema_version": "v1", "owner_already_set": True},
            )

        client.records.patch_v2_objects_object_records_record_id_(
            object="companies",
            record_id=company_record_id,
            data=build_patch_record_request(
                {
                    slug: [
                        {
                            "referenced_actor_id": person_record_id,
                            "referenced_actor_type": "workspace-member",
                        },
                    ],
                },
            ),
        )
        return ReliabilityEnvelope(
            success=True,
            partial_success=False,
            action="updated",
            record_id=company_record_id,
            warnings=[],
            skipped_fields=[],
            errors=[],
            meta={"output_schema_version": "v1"},
        )


def set_company_domain_if_empty(
    *,
    record_id: str,
    domain: str,
    apply: bool,
) -> ReliabilityEnvelope:
    """Fill ``domains`` on a Company only when currently empty (fill-only).

    Re-reads the Company immediately before writing and skips when ``domains``
    is non-empty. This is a best-effort fill-only write, not an atomic
    compare-and-swap: Attio does not expose a conditional-update primitive
    here, so a concurrent writer between our read and PATCH can still be
    clobbered. Callers that need strict "do not overwrite" semantics must
    serialize externally.

    Returns ``action="noop"`` with disambiguating meta on the three skip paths:
    - ``meta["preview"]=True`` — ``apply=False`` was passed
    - ``meta["domains_already_set"]=True`` — read showed a populated value
    - ``meta["domain_invalid"]=True`` — the supplied ``domain`` could not be
      formatted (caller passed garbage; treat as a resolution failure, not a race)

    Preview mode is a pure noop. Mirrors ``set_company_owner``.
    """
    if not apply:
        return ReliabilityEnvelope(
            success=True,
            partial_success=False,
            action="noop",
            record_id=record_id,
            warnings=[],
            skipped_fields=[],
            errors=[],
            meta={"output_schema_version": "v1", "preview": True},
        )

    with get_client() as client:
        current = client.records.get_v2_objects_object_records_record_id_(
            object="companies",
            record_id=record_id,
        )
        # Check if domains already has values
        domains_values = current.data.values.get("domains", [])
        if domains_values:
            # Domains already populated, don't overwrite
            return ReliabilityEnvelope(
                success=True,
                partial_success=False,
                action="noop",
                record_id=record_id,
                warnings=[],
                skipped_fields=[],
                errors=[],
                meta={"output_schema_version": "v1", "domains_already_set": True},
            )

        # Domains empty, PATCH with the new domain
        formatted_domains = format_company_domains(domain)
        if not formatted_domains:
            # Domain couldn't be formatted, return noop
            return ReliabilityEnvelope(
                success=True,
                partial_success=False,
                action="noop",
                record_id=record_id,
                warnings=[],
                skipped_fields=[],
                errors=[],
                meta={"output_schema_version": "v1", "domain_invalid": True},
            )

        try:
            client.records.patch_v2_objects_object_records_record_id_(
                object="companies",
                record_id=record_id,
                data=build_patch_record_request({"domains": formatted_domains}),
            )
        except AttioValidationError:
            # Some malformed domains can still get past upstream callers if the
            # structured response shape is unexpected. Keep the contract stable
            # by surfacing the same noop classification the formatter path uses.
            return ReliabilityEnvelope(
                success=True,
                partial_success=False,
                action="noop",
                record_id=record_id,
                warnings=[],
                skipped_fields=[],
                errors=[],
                meta={"output_schema_version": "v1", "domain_invalid": True},
            )
        return ReliabilityEnvelope(
            success=True,
            partial_success=False,
            action="updated",
            record_id=record_id,
            warnings=[],
            skipped_fields=[],
            errors=[],
            meta={"output_schema_version": "v1"},
        )
