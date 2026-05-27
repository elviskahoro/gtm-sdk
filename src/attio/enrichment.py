"""Attio Company domain enrichment via Exa (Multi-step orchestrator + Modal wrapper)."""

from __future__ import annotations

import itertools
import logging
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from libs.attio.client import get_client
from libs.attio.companies import set_company_domain_if_empty
from libs.attio.ext_tam import iter_company_ids_by_filter
from libs.attio.values import format_company_domains, looks_like_domain
from libs.exa.client import ExaAPIKeyMissingError
from libs.exa.errors import ExaError
from libs.exa.models import SearchInput
from libs.exa.search import search
from src.api_keys import inject_api_keys
from src.app import app, image
from src.secrets_bootstrap import bootstrap_secret, with_secrets

logger = logging.getLogger(__name__)


# --- Public types ---


class CompanyDomainOutcome(BaseModel):
    """Outcome of attempting to enrich one Company's domain."""

    model_config = ConfigDict(extra="forbid")

    company_record_id: str
    company_name: str
    # "patched": PATCH succeeded (apply=True, domain written).
    # "would_patch": apply=False, but Exa resolved a domain we would have written.
    # "noop_had_domain": company already had a domain (Exa never called).
    # "unresolved": Exa returned no domain (or domain Attio rejected as invalid).
    # "skipped_race": helper read non-empty domains between our scan and write.
    # "failed": per-row exception during processing.
    action: str
    resolved_domain: str | None = None
    exa_grounding_url: str | None = None
    exa_confidence: str | None = None
    exa_cost_dollars: float = 0.0
    error: str | None = None


class CompanyDomainBackfillReport(BaseModel):
    """Summary of Company domain backfill run."""

    model_config = ConfigDict(extra="forbid")

    patched: int = 0
    would_patch: int = 0  # preview-only: resolved a domain that we did not write
    noop_had_domain: int = 0
    unresolved: int = 0
    skipped_race: int = 0
    failed: int = 0
    outcomes: list[CompanyDomainOutcome] = []
    total_exa_cost_dollars: float = 0.0


# --- Private helpers ---


def _get_company_record(company_record_id: str) -> tuple[str, bool]:
    """Get company name and whether it has domains. Returns (name, has_domains)."""
    with get_client() as client:
        response = client.records.get_v2_objects_object_records_record_id_(
            object="companies",
            record_id=company_record_id,
        )
        name_values = response.data.values.get("name", [])
        name = None
        if name_values:
            name_value = name_values[0]
            if hasattr(name_value, "value"):
                name = name_value.value
            elif isinstance(name_value, str):
                name = name_value

        domains = response.data.values.get("domains", [])
        return name or f"ID:{company_record_id}", bool(domains)


def _resolve_domain_via_exa(
    company_name: str,
) -> tuple[str | None, str | None, str | None, float]:
    """Resolve primary domain for a company via Exa structured output.

    Returns:
        Tuple of (domain, grounding_url, confidence, cost_dollars).
        domain, grounding_url, confidence are None on miss; cost_dollars is always set.
    """
    response = search(
        SearchInput(
            query=f"What is the primary website domain for {company_name}?",
            category="company",
            type="auto",
            num_results=3,
            output_schema={
                "type": "object",
                "required": ["domain"],
                "properties": {
                    "domain": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
            },
            system_prompt=(
                "Prefer the company's official website domain. "
                "Skip social media, aggregators, and news articles."
            ),
        ),
    )

    cost_dollars = response.cost_dollars

    # Extract domain from structured output. Use ``is None`` instead of
    # truthiness so a content value of ``{}`` / ``""`` / ``0`` from a future
    # ``output_schema`` shape isn't conflated with "no output".
    if response.output is None or response.output.content is None:
        return None, None, None, cost_dollars

    content = response.output.content
    if not isinstance(content, dict):
        # Our resolver's output_schema declares ``content`` as an object with
        # a ``domain`` field. A non-dict response means Exa returned something
        # unexpected — surface as a miss rather than guessing.
        return None, None, None, cost_dollars

    raw_domain = content.get("domain")
    # Reject anything that isn't a non-empty trimmed string. The Attio write
    # path will reject these anyway, but catching here avoids classifying a
    # malformed Exa response as ``would_patch`` (apply=False) just because the
    # value happened to be truthy — e.g. a number, list, or whitespace string
    # (roborev finding).
    if not isinstance(raw_domain, str):
        return None, None, None, cost_dollars
    domain = raw_domain.strip()
    if not looks_like_domain(domain):
        # Reject obvious-garbage Exa results before sending them to Attio.
        # Catching shape errors here keeps the report classification accurate
        # (``unresolved``) rather than letting Attio's PATCH fail and surface
        # the row as a generic ``failed`` (roborev finding).
        return None, None, None, cost_dollars

    # Read confidence from the structured ``output.content["confidence"]`` first
    # — it's the value the outputSchema explicitly requested. Fall back to the
    # first citation's confidence only if the structured payload omitted it.
    # (``content`` is already narrowed to ``dict`` above.)
    confidence: str | None = None
    raw_confidence = content.get("confidence")
    if isinstance(raw_confidence, str):
        confidence = raw_confidence

    # Extract grounding URL (and the citation confidence as a fallback only).
    grounding_url: str | None = None
    grounding = response.output.grounding
    if grounding is not None and grounding.citations:
        first_citation = grounding.citations[0]
        grounding_url = first_citation.url
        if confidence is None:
            confidence = first_citation.confidence

    return domain, grounding_url, confidence, cost_dollars


# --- Public orchestrator ---


def backfill_company_domains_via_exa(
    *,
    ext_tam_filter: dict[str, Any] | None = None,
    company_ids: list[str] | None = None,
    limit: int | None = None,
    sleep_seconds: float = 0.0,
    apply: bool = False,
) -> CompanyDomainBackfillReport:
    """Resolve and PATCH missing ``domains`` on a target set of Companies.

    Target identification (exactly one must be set):
    - ``ext_tam_filter``: pivot via ext_tam, dedupe ``accounts[0]`` Company ids.
    - ``company_ids``: explicit list of Company record_ids.

    Per Company:
      1. GET Company; if ``domains`` non-empty → record ``noop_had_domain``.
      2. Resolve via Exa: ``category=company``, ``type=auto``, ``num_results=3``,
         ``output_schema={domain, confidence}``,
         ``system_prompt="Prefer the company's official website domain...\"``
      3. ``set_company_domain_if_empty(...)``; translate the envelope
         (``updated`` → ``patched``, ``noop`` → ``skipped_race``).

    Args:
        ext_tam_filter: Attio ext_tam filter dict (mutually exclusive with company_ids).
        company_ids: Explicit Company record_id list (mutually exclusive with ext_tam_filter).
        limit: Max Companies to process (None = no limit).
        sleep_seconds: Sleep between Company processing.
        apply: Whether to actually PATCH. If False, returns noop outcomes.

    Returns:
        CompanyDomainBackfillReport with outcomes and cost summary.

    Raises:
        ValueError: If neither or both selectors are set, or either is empty.
    """
    # Validate selectors here too — not just at the ``BackfillCompanyDomainsQuery``
    # Modal boundary. Direct programmatic callers (tests, scripts, library
    # users) bypass the wrapper validation, so an ``ext_tam_filter={}`` here
    # would otherwise silently page through the entire ext_tam table
    # (roborev finding). Same non-empty + exactly-one rules as the query model.
    has_filter = bool(ext_tam_filter)  # rejects None and {}
    has_ids = bool(company_ids)  # rejects None and []
    if has_filter == has_ids:
        raise ValueError(
            "Exactly one of ext_tam_filter or company_ids must be set (and non-empty)",
        )

    # Build company ID iterator. Both selector paths must dedupe — the
    # ext_tam pivot dedupes per-page; the explicit ``company_ids`` list is
    # caller-supplied so we strip + dedupe + validate here, preserving
    # first-occurrence order. Otherwise the same Company is processed
    # twice (cost/inflation) or a padded id like ``" rec_1 "`` is treated
    # as distinct from ``"rec_1"`` and fails the downstream Attio lookup
    # (roborev finding).
    if has_filter:
        assert ext_tam_filter is not None
        company_id_iter: Iterator[str] = iter_company_ids_by_filter(ext_tam_filter)
    else:
        seen: set[str] = set()
        deduped: list[str] = []
        for idx, raw_cid in enumerate(company_ids or []):
            # Runtime check defends against external callers (Modal payloads,
            # JSON deserialization) where Python types aren't enforced.
            # Pyright correctly notes the parameter is typed ``list[str]``;
            # the guard exists for misshaped runtime data.
            if not isinstance(raw_cid, str):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise ValueError(
                    f"company_ids[{idx}] must be a non-empty string",
                )
            cid = raw_cid.strip()
            if not cid:
                raise ValueError(
                    f"company_ids[{idx}] must be a non-empty string",
                )
            if cid not in seen:
                seen.add(cid)
                deduped.append(cid)
        if not deduped:
            # Defensive: ``has_ids`` was True (non-empty list) but every
            # entry was whitespace-only. Treat as caller error.
            raise ValueError("company_ids must contain at least one non-empty id")
        company_id_iter = iter(deduped)

    # Track outcomes
    report = CompanyDomainBackfillReport()
    processed = 0

    # Short-circuit ``limit=0`` BEFORE touching the iterator. Otherwise we'd
    # consume the first id from ``iter_company_ids_by_filter`` and trigger an
    # Attio query the caller explicitly asked us not to make (roborev finding).
    if limit is not None and limit <= 0:
        return report

    # Bound the iterator with ``islice`` so we never consume past the cap.
    # A simple ``if processed >= limit: break`` in the loop body would still
    # advance the iterator one extra step (Python's ``for`` calls ``next()``
    # before evaluating the body), which on a filtered ext_tam path can
    # trigger an extra Attio page fetch (roborev finding).
    if limit is not None:
        company_id_iter = itertools.islice(company_id_iter, limit)

    # Sleep BEFORE processing each record (except the first) rather than
    # after. This way the throttle fires once per inter-record gap and the
    # last record never pays a trailing wait — irrespective of whether the
    # iterator exhausts on its own or hits a limit (roborev finding).
    import time

    for company_record_id in company_id_iter:
        if processed > 0 and sleep_seconds > 0:
            time.sleep(sleep_seconds)

        # ``processed`` is incremented in the ``finally`` so every outcome
        # branch contributes exactly one count (noop_had_domain / unresolved
        # / would_patch / patched / skipped_race / failed).
        outcome_recorded = False
        try:
            try:
                _process_one_company(
                    company_record_id=company_record_id,
                    apply=apply,
                    report=report,
                )
                outcome_recorded = True
            except (ExaAPIKeyMissingError, ExaError):
                # Non-recoverable failures from Exa (missing credential, auth,
                # bad-request, rate limit, server error, AND any other typed
                # HTTP status that ``from_http_status`` mapped to the base
                # ``ExaError``) are NOT per-row errors — they recur for every
                # record and just burn cost / credentials. Short-circuit so
                # the operator sees the real failure instead of N false
                # "failed" outcomes (roborev finding).
                #
                # ``ExaAPIKeyMissingError`` is a ``ValueError`` subclass (not
                # an ``ExaError``) so it must be listed explicitly to avoid
                # being swallowed by the generic per-row handler below.
                raise
            except Exception as exc:
                outcome = CompanyDomainOutcome(
                    company_record_id=company_record_id,
                    company_name=f"ID:{company_record_id}",
                    action="failed",
                    error=str(exc),
                )
                report.outcomes.append(outcome)
                report.failed += 1
                outcome_recorded = True
                logger.exception("Failed to process company %s", company_record_id)
        finally:
            if outcome_recorded:
                processed += 1

    return report


def _process_one_company(
    *,
    company_record_id: str,
    apply: bool,
    report: CompanyDomainBackfillReport,
) -> None:
    """Process a single Company, mutating ``report`` with one outcome.

    Extracted from the main loop so the sleep/throttle logic in the caller
    can run once per company regardless of which outcome branch fires.
    """
    company_name, has_domains = _get_company_record(company_record_id)

    if has_domains:
        report.outcomes.append(
            CompanyDomainOutcome(
                company_record_id=company_record_id,
                company_name=company_name,
                action="noop_had_domain",
            ),
        )
        report.noop_had_domain += 1
        return

    domain, grounding_url, confidence, exa_cost = _resolve_domain_via_exa(company_name)
    report.total_exa_cost_dollars += exa_cost

    if not domain:
        report.outcomes.append(
            CompanyDomainOutcome(
                company_record_id=company_record_id,
                company_name=company_name,
                action="unresolved",
                exa_cost_dollars=exa_cost,
            ),
        )
        report.unresolved += 1
        return

    if not apply:
        # Run the same domain-format check the write path uses, so preview
        # counts match what an apply run would report (roborev finding):
        # a malformed domain would become ``unresolved`` under apply via
        # ``set_company_domain_if_empty``'s ``domain_invalid`` noop path;
        # we mirror that classification here instead of marking it as
        # ``would_patch`` and silently disagreeing.
        if not format_company_domains(domain):
            report.outcomes.append(
                CompanyDomainOutcome(
                    company_record_id=company_record_id,
                    company_name=company_name,
                    action="unresolved",
                    resolved_domain=domain,
                    exa_grounding_url=grounding_url,
                    exa_confidence=confidence,
                    exa_cost_dollars=exa_cost,
                ),
            )
            report.unresolved += 1
            return

        # Preview: record the resolution as ``would_patch`` so the report
        # distinguishes "already had a domain" from "would have written this".
        report.outcomes.append(
            CompanyDomainOutcome(
                company_record_id=company_record_id,
                company_name=company_name,
                action="would_patch",
                resolved_domain=domain,
                exa_grounding_url=grounding_url,
                exa_confidence=confidence,
                exa_cost_dollars=exa_cost,
            ),
        )
        report.would_patch += 1
        return

    envelope = set_company_domain_if_empty(
        record_id=company_record_id,
        domain=domain,
        apply=True,
    )

    # Translate envelope to outcome. ``noop`` covers three distinct paths in
    # set_company_domain_if_empty; the meta flag disambiguates:
    #   domain_invalid       → unresolved (Exa returned garbage)
    #   domains_already_set  → noop_had_domain (read-after-resolve race)
    #   (none of the above)  → skipped_race fallback
    if envelope.action == "updated":
        action = "patched"
        report.patched += 1
    elif envelope.action == "noop":
        meta = envelope.meta or {}
        if meta.get("domain_invalid"):
            action = "unresolved"
            report.unresolved += 1
        elif meta.get("domains_already_set"):
            action = "noop_had_domain"
            report.noop_had_domain += 1
        else:
            action = "skipped_race"
            report.skipped_race += 1
    else:
        action = "noop_had_domain"
        report.noop_had_domain += 1

    report.outcomes.append(
        CompanyDomainOutcome(
            company_record_id=company_record_id,
            company_name=company_name,
            action=action,
            resolved_domain=domain,
            exa_grounding_url=grounding_url,
            exa_confidence=confidence,
            exa_cost_dollars=exa_cost,
        ),
    )


# --- Modal wrapper ---


class BackfillCompanyDomainsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ext_tam_filter: dict[str, Any] | None = None
    company_ids: list[str] | None = None
    limit: int | None = None
    sleep_seconds: float = 0.0
    apply: bool = False

    @field_validator("sleep_seconds")
    @classmethod
    def validate_sleep_seconds(cls, v: float) -> float:
        if v < 0:
            raise ValueError("sleep_seconds must be non-negative")
        return v

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("limit must be a positive integer or None")
        return v

    @model_validator(mode="after")
    def validate_exactly_one_selector(self) -> BackfillCompanyDomainsQuery:
        """Enforce the selector contract at the model boundary so ``--json``
        payloads fail fast instead of crashing inside the Modal function.

        Rejects empty selectors *independently* before the exactly-one check —
        otherwise a payload like ``{"ext_tam_filter": {}, "company_ids": ["x"]}``
        would silently pass the exactly-one rule (because ``{}`` is falsy)
        and hide a clearly malformed request (roborev finding).
        """
        # Empty ``ext_tam_filter`` would page through every ext_tam record;
        # an empty dict is almost certainly a caller bug, not "no filter".
        if self.ext_tam_filter is not None and not self.ext_tam_filter:
            raise ValueError("ext_tam_filter must be a non-empty dict")
        # Empty ``company_ids`` list is similarly always a caller bug.
        if self.company_ids is not None and not self.company_ids:
            raise ValueError("company_ids must be a non-empty list")

        has_filter = self.ext_tam_filter is not None
        has_ids = self.company_ids is not None
        if has_filter == has_ids:
            raise ValueError(
                "Exactly one of ext_tam_filter or company_ids must be set",
            )
        if has_ids:
            assert self.company_ids is not None
            normalized: list[str] = []
            for idx, cid in enumerate(self.company_ids):
                stripped = cid.strip()
                # Pydantic's ``list[str]`` already rejects non-string entries;
                # we still need to catch empty/whitespace-only strings here
                # because they pass the type check.
                if not stripped:
                    raise ValueError(
                        f"company_ids[{idx}] must be a non-empty string",
                    )
                normalized.append(stripped)
            # Normalize the list in place so downstream consumers see the
            # canonical (stripped) ids — otherwise " rec_1 " passes validation
            # but the Attio lookup uses the padded form and fails.
            self.company_ids = normalized
        return self


@app.function(image=image, secrets=[bootstrap_secret()])
@with_secrets("ATTIO_API_KEY", "EXA_API_KEY")
def attio_backfill_company_domains(
    payload: dict[str, Any],
    api_keys: dict[str, str] | None = None,
) -> CompanyDomainBackfillReport:
    """Backfill missing domains on Attio Companies via Exa (Modal wrapper).

    Args:
        payload: Dict matching BackfillCompanyDomainsQuery.
        api_keys: Optional API key overrides.

    Returns:
        CompanyDomainBackfillReport with outcomes and cost.
    """
    with inject_api_keys(api_keys or {}):
        query = BackfillCompanyDomainsQuery.model_validate(payload)
        return backfill_company_domains_via_exa(**query.model_dump())
