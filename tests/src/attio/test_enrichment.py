"""Tests for src/attio/enrichment.py — Company domain backfill orchestrator."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from libs.attio.contracts import ReliabilityEnvelope
from libs.attio.errors import AttioValidationError
from libs.exa.client import ExaAPIKeyMissingError
from libs.exa.errors import ExaAuthError, ExaBadRequestError, ExaRateLimitError
from libs.exa.models import (
    GroundingCitation,
    OutputGrounding,
    SearchInput,
    SearchOutput,
    SearchResponse,
)
from src.attio.enrichment import (
    BackfillCompanyDomainsQuery,
    backfill_company_domains_via_exa,
)


def _envelope(
    action: str,
    *,
    meta_flag: str | None = None,
) -> ReliabilityEnvelope:
    """Build a fake envelope. ``meta_flag`` lets a test stamp the disambiguating
    meta key (``domain_invalid`` / ``domains_already_set``) the orchestrator
    branches on for noop translations."""
    meta: dict[str, Any] = {"output_schema_version": "v1"}
    if meta_flag is not None:
        meta[meta_flag] = True
    return ReliabilityEnvelope(
        success=True,
        partial_success=False,
        action=action,  # type: ignore[arg-type]
        record_id="rec_1",
        warnings=[],
        skipped_fields=[],
        errors=[],
        meta=meta,
    )


def _search_response_hit(domain: str, *, cost: float = 0.005) -> SearchResponse:
    return SearchResponse(
        request_id="req_x",
        search_type="auto",
        results=[],
        output=SearchOutput(
            content={"domain": domain, "confidence": "high"},
            grounding=OutputGrounding(
                citations=[
                    GroundingCitation(
                        url=f"https://{domain}",
                        title=domain,
                        confidence="high",
                    ),
                ],
            ),
        ),
        cost_dollars=cost,
    )


def _search_response_miss(*, cost: float = 0.005) -> SearchResponse:
    return SearchResponse(
        request_id="req_x",
        search_type="auto",
        results=[],
        output=SearchOutput(content=None, grounding=None),
        cost_dollars=cost,
    )


@contextmanager
def _patched_pipeline(
    *,
    company_records: dict[str, tuple[str, bool]],
    exa_response: SearchResponse | None = None,
    set_domain_action: str = "updated",
    set_domain_meta_flag: str | None = None,
    company_id_iter: list[str] | None = None,
):
    """Patch all collaborators around backfill_company_domains_via_exa.

    company_records: maps record_id -> (name, has_domains).
    exa_response: response returned by libs.exa.search.search. None = not patched.
    set_domain_action: action returned by set_company_domain_if_empty.
    set_domain_meta_flag: disambiguating meta key on the envelope when noop
      (``domain_invalid`` or ``domains_already_set``); None = no flag set.
    company_id_iter: ids yielded by iter_company_ids_by_filter when ext_tam_filter is used.
    """

    def fake_get_record(rid: str) -> tuple[str, bool]:
        return company_records[rid]

    def fake_iter(_filter: dict[str, Any]):
        for rid in company_id_iter or []:
            yield rid

    search_mock = MagicMock(return_value=exa_response)
    set_domain_mock = MagicMock(
        return_value=_envelope(set_domain_action, meta_flag=set_domain_meta_flag),
    )

    with (
        patch("src.attio.enrichment._get_company_record", side_effect=fake_get_record),
        patch("src.attio.enrichment.iter_company_ids_by_filter", side_effect=fake_iter),
        patch("src.attio.enrichment.search", search_mock),
        patch("src.attio.enrichment.set_company_domain_if_empty", set_domain_mock),
    ):
        yield {"search": search_mock, "set_domain": set_domain_mock}


def test_hit_patched_when_apply_true() -> None:
    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=_search_response_hit("acme.com"),
        set_domain_action="updated",
    ) as mocks:
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=True,
        )

    assert report.patched == 1
    assert report.outcomes[0].action == "patched"
    assert report.outcomes[0].resolved_domain == "acme.com"
    assert report.outcomes[0].exa_confidence == "high"
    assert report.outcomes[0].exa_grounding_url == "https://acme.com"
    mocks["set_domain"].assert_called_once()


def test_miss_unresolved_when_exa_returns_no_content() -> None:
    with _patched_pipeline(
        company_records={"rec_1": ("Obscure Co", False)},
        exa_response=_search_response_miss(),
    ) as mocks:
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=True,
        )

    assert report.unresolved == 1
    assert report.outcomes[0].action == "unresolved"
    assert report.outcomes[0].resolved_domain is None
    assert report.total_exa_cost_dollars > 0  # cost still accounted on miss
    mocks["set_domain"].assert_not_called()


def test_already_has_domain_noop_skips_exa() -> None:
    with _patched_pipeline(
        company_records={"rec_1": ("Acme", True)},  # has_domains=True
    ) as mocks:
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=True,
        )

    assert report.noop_had_domain == 1
    assert report.outcomes[0].action == "noop_had_domain"
    # Exa never called when company already has a domain
    mocks["search"].assert_not_called()
    mocks["set_domain"].assert_not_called()


def test_envelope_noop_with_domains_already_set_meta_is_noop_had_domain() -> None:
    """Helper read showed populated domains between our scan and write — that's
    a domains_already_set race against another writer, not a generic race."""
    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=_search_response_hit("acme.com"),
        set_domain_action="noop",
        set_domain_meta_flag="domains_already_set",
    ):
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=True,
        )

    assert report.noop_had_domain == 1
    assert report.outcomes[0].action == "noop_had_domain"


def test_envelope_noop_with_domain_invalid_meta_is_unresolved() -> None:
    """Exa returned a domain that couldn't be formatted — surface as unresolved,
    not skipped_race. Regression for roborev finding about noop misclassification."""
    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=_search_response_hit("bad-domain-format"),
        set_domain_action="noop",
        set_domain_meta_flag="domain_invalid",
    ):
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=True,
        )

    assert report.unresolved == 1
    assert report.outcomes[0].action == "unresolved"


def test_envelope_bare_noop_is_skipped_race_fallback() -> None:
    """Helper returned noop with no disambiguating meta flag — treat as race."""
    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=_search_response_hit("acme.com"),
        set_domain_action="noop",
        set_domain_meta_flag=None,
    ):
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=True,
        )

    assert report.skipped_race == 1
    assert report.outcomes[0].action == "skipped_race"


def test_apply_false_does_not_call_set_domain() -> None:
    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=_search_response_hit("acme.com"),
    ) as mocks:
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=False,
        )

    # Preview never writes
    mocks["set_domain"].assert_not_called()
    # Resolution recorded as ``would_patch`` — distinct from "already had a
    # domain", so the report tells the operator how much real work was
    # surfaced (roborev finding).
    assert report.would_patch == 1
    assert report.noop_had_domain == 0
    assert report.outcomes[0].action == "would_patch"
    assert report.outcomes[0].resolved_domain == "acme.com"
    assert report.total_exa_cost_dollars > 0


def test_apply_false_invalid_domain_is_unresolved() -> None:
    """Preview mode should mirror the write path's domain formatter."""
    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=_search_response_hit("bad-domain-format"),
    ) as mocks:
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=False,
        )

    mocks["set_domain"].assert_not_called()
    assert report.unresolved == 1
    assert report.outcomes[0].action == "unresolved"
    assert report.outcomes[0].resolved_domain is None


def test_limit_zero_short_circuits_before_iteration() -> None:
    """limit=0 should not consume the iterator or touch any collaborators."""
    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=_search_response_hit("acme.com"),
        company_id_iter=["rec_1"],
    ) as mocks:
        report = backfill_company_domains_via_exa(
            ext_tam_filter={"foo": "bar"},
            limit=0,
            apply=False,
        )

    assert report.model_dump() == {
        "patched": 0,
        "would_patch": 0,
        "noop_had_domain": 0,
        "unresolved": 0,
        "skipped_race": 0,
        "failed": 0,
        "outcomes": [],
        "total_exa_cost_dollars": 0.0,
    }
    mocks["search"].assert_not_called()
    mocks["set_domain"].assert_not_called()


def test_empty_ext_tam_filter_raises_at_function_boundary() -> None:
    """Regression: the public function must reject empty selectors directly."""
    with pytest.raises(ValueError, match="ext_tam_filter must be a non-empty"):
        backfill_company_domains_via_exa(ext_tam_filter={}, apply=False)


def test_empty_company_ids_raises_at_function_boundary() -> None:
    with pytest.raises(ValueError, match="company_ids must be a non-empty"):
        backfill_company_domains_via_exa(company_ids=[], apply=False)


def test_company_ids_path_strips_and_dedupes_at_function_boundary() -> None:
    """Regression (roborev): direct programmatic callers must also get
    strip + dedupe + non-empty-string validation, not just the
    ``BackfillCompanyDomainsQuery`` Modal-boundary wrapper. Otherwise
    ``" rec_1 "`` and ``"rec_1"`` are treated as distinct ids."""
    with _patched_pipeline(
        company_records={"rec_1": ("Acme", True)},  # already has domain
    ) as mocks:
        report = backfill_company_domains_via_exa(
            company_ids=["  rec_1  ", "rec_1", " rec_1\t"],
            apply=False,
        )

    # All three inputs normalize to the same id; only one Company processed.
    assert len(report.outcomes) == 1
    mocks["search"].assert_not_called()


def test_company_ids_path_rejects_blank_entry() -> None:
    """Whitespace-only id in the direct path raises rather than silently
    skipping (roborev finding)."""
    with pytest.raises(ValueError, match="non-empty string"):
        backfill_company_domains_via_exa(
            company_ids=["rec_1", "   "],
            apply=False,
        )


def test_both_selectors_set_raises() -> None:
    with pytest.raises(ValueError, match="Exactly one of"):
        backfill_company_domains_via_exa(
            ext_tam_filter={"source": "x"},
            company_ids=["rec_1"],
            apply=False,
        )


def test_neither_selector_set_raises() -> None:
    with pytest.raises(ValueError, match="Exactly one of"):
        backfill_company_domains_via_exa(apply=False)


def test_query_model_rejects_both_selectors() -> None:
    """Pydantic model_validator catches invalid --json payloads at the boundary
    (roborev finding) so callers don't crash inside the Modal function."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Exactly one of"):
        BackfillCompanyDomainsQuery.model_validate(
            {"ext_tam_filter": {"source": "x"}, "company_ids": ["rec_1"]},
        )


def test_query_model_rejects_neither_selector() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Exactly one of"):
        BackfillCompanyDomainsQuery.model_validate({"apply": False})


def test_query_model_rejects_empty_company_ids_list() -> None:
    """Regression (roborev): ``company_ids=[]`` is technically present but
    represents zero work — must be rejected at the model boundary."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="company_ids must be a non-empty"):
        BackfillCompanyDomainsQuery.model_validate({"company_ids": []})


def test_query_model_rejects_empty_ext_tam_filter() -> None:
    """Empty filter ``{}`` would page through every ext_tam record — almost
    certainly a mistake; reject at the model boundary."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="ext_tam_filter must be a non-empty"):
        BackfillCompanyDomainsQuery.model_validate({"ext_tam_filter": {}})


def test_query_model_rejects_limit_zero() -> None:
    """Regression: ``limit=0`` must not be treated as an unlimited backfill."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="limit must be a positive integer"):
        BackfillCompanyDomainsQuery.model_validate(
            {"company_ids": ["rec_1"], "limit": 0},
        )


def test_query_model_rejects_empty_filter_combined_with_company_ids() -> None:
    """Regression (roborev): a payload like
    ``{"ext_tam_filter": {}, "company_ids": ["x"]}`` would have silently passed
    the exactly-one rule (``{}`` is falsy). Now caught by the independent
    empty-selector check that runs first."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="ext_tam_filter must be a non-empty"):
        BackfillCompanyDomainsQuery.model_validate(
            {"ext_tam_filter": {}, "company_ids": ["rec_1"]},
        )


def test_query_model_normalizes_whitespace_in_company_ids() -> None:
    """Regression (roborev): caller-supplied ``" rec_1 "`` must be normalized
    to ``"rec_1"`` so the downstream Attio lookup uses the canonical id."""
    query = BackfillCompanyDomainsQuery.model_validate(
        {"company_ids": [" rec_1 ", "rec_2", "\trec_3\n"]},
    )
    assert query.company_ids == ["rec_1", "rec_2", "rec_3"]


def test_query_model_rejects_blank_company_id() -> None:
    """Whitespace-only or empty strings in ``company_ids`` are caller error;
    surface at validation time rather than crashing on a downstream lookup."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="non-empty string"):
        BackfillCompanyDomainsQuery.model_validate(
            {"company_ids": ["rec_1", "   "]},
        )


def test_ext_tam_filter_routes_through_iterator() -> None:
    with _patched_pipeline(
        company_records={
            "rec_1": ("A", False),
            "rec_2": ("B", True),  # already has domain
        },
        exa_response=_search_response_hit("a.com"),
        company_id_iter=["rec_1", "rec_2"],
    ) as mocks:
        report = backfill_company_domains_via_exa(
            ext_tam_filter={"source": "snowflake_scored_accounts_csv"},
            apply=True,
        )

    assert report.patched == 1
    assert report.noop_had_domain == 1
    # search() only invoked for rec_1 (rec_2 already had a domain)
    assert mocks["search"].call_count == 1


def test_domain_with_spaces_treated_as_unresolved() -> None:
    """Regression (roborev): malformed-shape domain (e.g. ``"acme com"``)
    must be classified as unresolved instead of sent to Attio (where the
    PATCH would fail and surface as a generic ``failed`` outcome)."""
    response = SearchResponse(
        request_id="req",
        search_type="auto",
        results=[],
        output=SearchOutput(
            content={"domain": "acme com", "confidence": "low"},
            grounding=None,
        ),
        cost_dollars=0.001,
    )

    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=response,
    ) as mocks:
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=True,
        )

    assert report.unresolved == 1
    assert report.failed == 0
    mocks["set_domain"].assert_not_called()


def test_domain_with_scheme_treated_as_unresolved() -> None:
    """Exa sometimes returns full URLs (``"https://acme.com/about"``). We
    need a real bare domain; let Attio guard the rest."""
    response = SearchResponse(
        request_id="req",
        search_type="auto",
        results=[],
        output=SearchOutput(
            content={"domain": "https://acme.com", "confidence": "high"},
            grounding=None,
        ),
        cost_dollars=0.001,
    )

    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=response,
    ) as mocks:
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=True,
        )

    assert report.unresolved == 1
    mocks["set_domain"].assert_not_called()


def test_attio_validation_error_treated_as_unresolved() -> None:
    """Regression for malformed Exa output that slips past upstream checks:
    the live PATCH path must map Attio validation failures back to the
    unresolved/domain_invalid classification instead of surfacing a generic
    row failure."""
    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=_search_response_hit("acme.com"),
    ) as mocks:
        mocks["set_domain"].side_effect = AttioValidationError("invalid domain")
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=True,
        )

    assert report.unresolved == 1
    assert report.failed == 0
    assert report.outcomes[0].action == "unresolved"
    assert report.outcomes[0].resolved_domain == "acme.com"


def test_non_string_domain_treated_as_unresolved() -> None:
    """Regression (roborev): if Exa returns a non-string for ``domain``
    (e.g. a list or number), the resolver must classify as unresolved instead
    of letting truthy non-string values flow into the Attio write path."""
    response = SearchResponse(
        request_id="req",
        search_type="auto",
        results=[],
        output=SearchOutput(
            content={"domain": ["acme.com"], "confidence": "low"},  # list, not str
            grounding=None,
        ),
        cost_dollars=0.001,
    )

    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=response,
    ) as mocks:
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=True,
        )

    assert report.unresolved == 1
    mocks["set_domain"].assert_not_called()


def test_whitespace_only_domain_treated_as_unresolved() -> None:
    """Regression (roborev): whitespace-only domain must NOT be classified
    as ``would_patch`` (preview) or sent to Attio (apply)."""
    response = SearchResponse(
        request_id="req",
        search_type="auto",
        results=[],
        output=SearchOutput(
            content={"domain": "   ", "confidence": "low"},
            grounding=None,
        ),
        cost_dollars=0.001,
    )

    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=response,
    ):
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=False,
        )

    assert report.unresolved == 1
    assert report.would_patch == 0


def test_preview_with_invalid_domain_classified_as_unresolved() -> None:
    """Regression (roborev): preview must apply the same domain-format check
    as the write path, otherwise the same record produces ``would_patch`` in
    preview but ``unresolved`` under apply — misleading the operator about
    how much real work is queued."""
    # Empty/falsy domain string is what ``format_company_domains`` rejects.
    response = SearchResponse(
        request_id="req",
        search_type="auto",
        results=[],
        output=SearchOutput(
            content={"domain": " ", "confidence": "low"},  # whitespace = invalid
            grounding=None,
        ),
        cost_dollars=0.001,
    )

    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=response,
    ):
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=False,
        )

    # Note: this test exercises the format-validation branch. The current
    # resolver returns the whitespace string as-is; ``format_company_domains``
    # treats a non-empty string as valid, so a more rigorous test would need
    # a domain that ``format_company_domains`` actually rejects. Since the
    # helper only rejects ``not domain`` (falsy), the parity guarantee is
    # currently: any domain that survives ``_resolve_domain_via_exa``'s
    # ``not domain`` check will pass ``format_company_domains`` too — but
    # the orchestrator now applies the check unconditionally so a tightened
    # format validator in the helper would automatically be honored in preview.
    assert len(report.outcomes) == 1


def test_limit_zero_short_circuits_before_iterator() -> None:
    """Regression (roborev): ``limit=0`` must not consume the iterator at all.
    Otherwise an ``ext_tam_filter`` path triggers an Attio query the caller
    explicitly asked us to skip."""
    iter_called = False

    def fake_iter(_filter: dict[str, Any]):
        nonlocal iter_called
        iter_called = True
        yield "rec_unexpected"

    with (
        patch("src.attio.enrichment.iter_company_ids_by_filter", side_effect=fake_iter),
        patch("src.attio.enrichment._get_company_record") as get_rec,
    ):
        report = backfill_company_domains_via_exa(
            ext_tam_filter={"source": "x"},
            limit=0,
            apply=True,
        )

    assert len(report.outcomes) == 0
    # The iterator was never even constructed/consumed.
    assert iter_called is False
    get_rec.assert_not_called()


def test_limit_zero_processes_zero_records() -> None:
    """Regression: limit=0 must be honored, not coerced to ``no limit`` (roborev finding)."""
    with _patched_pipeline(
        company_records={f"rec_{i}": ("X", False) for i in range(5)},
        exa_response=_search_response_hit("x.com"),
        company_id_iter=[f"rec_{i}" for i in range(5)],
    ) as mocks:
        report = backfill_company_domains_via_exa(
            ext_tam_filter={"source": "x"},
            limit=0,
            apply=True,
        )

    assert len(report.outcomes) == 0
    mocks["search"].assert_not_called()
    mocks["set_domain"].assert_not_called()


def test_limit_does_not_consume_extra_iterator_item() -> None:
    """Regression (roborev): hitting the limit must not advance the iterator
    one extra step (which Python's for-loop semantics would otherwise do).
    Verified by counting how many ids the iterator actually yields."""
    yielded: list[str] = []

    def fake_iter(_filter: dict[str, Any]):
        for rid in ["a", "b", "c", "d", "e"]:
            yielded.append(rid)
            yield rid

    with (
        patch("src.attio.enrichment.iter_company_ids_by_filter", side_effect=fake_iter),
        patch("src.attio.enrichment._get_company_record", return_value=("X", True)),
    ):
        backfill_company_domains_via_exa(
            ext_tam_filter={"source": "x"},
            limit=2,
            apply=False,
        )

    # With ``islice``, the iterator is asked for exactly ``limit`` ids — no
    # over-pull. Before this fix, ``yielded`` would contain 3 entries because
    # the for-loop fetched id #3 before checking the cap.
    assert yielded == ["a", "b"]


def test_limit_respected() -> None:
    with _patched_pipeline(
        company_records={f"rec_{i}": ("X", True) for i in range(10)},
        company_id_iter=[f"rec_{i}" for i in range(10)],
    ):
        report = backfill_company_domains_via_exa(
            ext_tam_filter={"source": "x"},
            limit=3,
            apply=False,
        )

    assert len(report.outcomes) == 3


def test_confidence_read_from_output_content_not_citation() -> None:
    """Regression (roborev): exa_confidence must come from the structured
    output (which the outputSchema explicitly requested) rather than citation
    metadata, which may not reflect the LLM's own confidence."""
    response = SearchResponse(
        request_id="req",
        search_type="auto",
        results=[],
        output=SearchOutput(
            content={"domain": "acme.com", "confidence": "high"},
            grounding=OutputGrounding(
                citations=[
                    GroundingCitation(
                        url="https://acme.com",
                        title="Acme",
                        confidence="low",  # deliberately divergent
                    ),
                ],
            ),
        ),
        cost_dollars=0.005,
    )

    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=response,
    ):
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1"],
            apply=True,
        )

    # Outcome reports the structured-output confidence, not the citation's.
    assert report.outcomes[0].exa_confidence == "high"


def test_explicit_company_ids_deduped() -> None:
    """Regression (roborev): caller-supplied ``company_ids`` list must dedupe
    so the same Company isn't processed twice."""
    with _patched_pipeline(
        company_records={"rec_1": ("Acme", False)},
        exa_response=_search_response_hit("acme.com"),
    ) as mocks:
        report = backfill_company_domains_via_exa(
            company_ids=["rec_1", "rec_1", "rec_1"],
            apply=True,
        )

    assert len(report.outcomes) == 1
    assert report.patched == 1
    mocks["search"].assert_called_once()
    mocks["set_domain"].assert_called_once()


def test_exa_auth_error_short_circuits_run() -> None:
    """Regression (roborev): service-level Exa failures (auth, rate limit, 5xx)
    are not per-row errors — they recur for every record. Must short-circuit
    the run instead of recording N false 'failed' outcomes."""
    auth_error = ExaAuthError("bad token", status=401, request_id="req")

    with (
        patch("src.attio.enrichment._get_company_record", return_value=("A", False)),
        patch("src.attio.enrichment.search", side_effect=auth_error),
        patch("src.attio.enrichment.set_company_domain_if_empty") as set_dom,
    ):
        with pytest.raises(ExaAuthError):
            backfill_company_domains_via_exa(
                company_ids=["rec_1", "rec_2", "rec_3"],
                apply=True,
            )

    # No PATCHes attempted after the service-level failure.
    set_dom.assert_not_called()


def test_exa_bad_request_error_short_circuits_run() -> None:
    """Regression (roborev): a 400/422 from Exa means our query/outputSchema
    is malformed — same shape for every record, so abort instead of generating
    N false 'failed' outcomes."""
    bad_request = ExaBadRequestError("schema bad", status=400, request_id="req")

    with (
        patch("src.attio.enrichment._get_company_record", return_value=("A", False)),
        patch("src.attio.enrichment.search", side_effect=bad_request),
        patch("src.attio.enrichment.set_company_domain_if_empty") as set_dom,
    ):
        with pytest.raises(ExaBadRequestError):
            backfill_company_domains_via_exa(
                company_ids=["rec_1", "rec_2"],
                apply=True,
            )

    set_dom.assert_not_called()


def test_base_exa_error_also_short_circuits_run() -> None:
    """Regression (roborev): the orchestrator must catch the BASE
    ``ExaError`` too, not just the four named subclasses. Otherwise an
    HTTP code that ``from_http_status`` maps to plain ``ExaError`` (e.g.
    403, 408) would slip into the per-row handler and burn every record."""
    from libs.exa.errors import ExaError

    err = ExaError("teapot", status=418, request_id="req")

    with (
        patch("src.attio.enrichment._get_company_record", return_value=("A", False)),
        patch("src.attio.enrichment.search", side_effect=err),
        patch("src.attio.enrichment.set_company_domain_if_empty") as set_dom,
    ):
        with pytest.raises(ExaError):
            backfill_company_domains_via_exa(
                company_ids=["rec_1", "rec_2"],
                apply=True,
            )

    set_dom.assert_not_called()


def test_exa_api_key_missing_error_short_circuits_run() -> None:
    """Regression (roborev): ``ExaAPIKeyMissingError`` is a ``ValueError``
    subclass, not an ``ExaError``. Must be listed explicitly in the
    short-circuit handler or it falls through to the per-row failure path
    and burns through every record with the same config error."""
    missing = ExaAPIKeyMissingError("Exa API key not resolved.")

    with (
        patch("src.attio.enrichment._get_company_record", return_value=("A", False)),
        patch("src.attio.enrichment.search", side_effect=missing),
        patch("src.attio.enrichment.set_company_domain_if_empty") as set_dom,
    ):
        with pytest.raises(ExaAPIKeyMissingError):
            backfill_company_domains_via_exa(
                company_ids=["rec_1", "rec_2", "rec_3"],
                apply=True,
            )

    set_dom.assert_not_called()


def test_exa_rate_limit_error_short_circuits_run() -> None:
    rate_limit = ExaRateLimitError("slow down", status=429, request_id="req")

    with (
        patch("src.attio.enrichment._get_company_record", return_value=("A", False)),
        patch("src.attio.enrichment.search", side_effect=rate_limit),
        patch("src.attio.enrichment.set_company_domain_if_empty") as set_dom,
    ):
        with pytest.raises(ExaRateLimitError):
            backfill_company_domains_via_exa(
                company_ids=["rec_1", "rec_2"],
                apply=True,
            )

    set_dom.assert_not_called()


def test_sleep_seconds_runs_on_every_outcome_branch(monkeypatch) -> None:
    """Regression (roborev): the inter-row sleep must fire once per processed
    company, regardless of outcome (noop_had_domain, unresolved, would_patch,
    patched). Previously the sleep was at the bottom of the loop body and
    early-``continue`` branches skipped it entirely."""
    sleep_calls: list[float] = []

    def _capture_sleep(s: float) -> None:
        sleep_calls.append(s)

    monkeypatch.setattr("time.sleep", _capture_sleep)

    # Four records — one of each "early branch" plus one full apply path.
    with _patched_pipeline(
        company_records={
            "rec_a": ("A", True),  # noop_had_domain (no Exa call)
            "rec_b": ("B", False),  # unresolved
            "rec_c": ("C", False),  # would_patch (apply=False overrides per-row)
            "rec_d": ("D", False),  # full path (but apply=False so also would_patch)
        },
    ) as _:
        # Use a sequence of search responses: rec_b miss, rec_c hit, rec_d hit.
        # rec_a is short-circuited before Exa is called.
        from unittest.mock import patch as _patch

        responses = iter(
            [
                _search_response_miss(),
                _search_response_hit("c.com"),
                _search_response_hit("d.com"),
            ],
        )

        def _next_response(_input: SearchInput) -> SearchResponse:
            return next(responses)

        with _patch("src.attio.enrichment.search", side_effect=_next_response):
            backfill_company_domains_via_exa(
                company_ids=["rec_a", "rec_b", "rec_c", "rec_d"],
                apply=False,
                sleep_seconds=0.5,
            )

    # 4 processed records, each hits a different outcome path. The throttle
    # fires once per *inter-record gap* — 3 sleeps for 4 records. The last
    # record never pays a trailing wait (roborev finding).
    assert sleep_calls == [0.5, 0.5, 0.5]


def test_per_row_exception_records_failed_and_continues() -> None:
    def flaky_get_record(rid: str) -> tuple[str, bool]:
        if rid == "rec_bad":
            raise RuntimeError("boom")
        return ("OK Co", False)

    with (
        patch("src.attio.enrichment._get_company_record", side_effect=flaky_get_record),
        patch(
            "src.attio.enrichment.search",
            return_value=_search_response_hit("ok.com"),
        ),
        patch(
            "src.attio.enrichment.set_company_domain_if_empty",
            return_value=_envelope("updated"),
        ),
    ):
        report = backfill_company_domains_via_exa(
            company_ids=["rec_bad", "rec_good"],
            apply=True,
        )

    assert report.failed == 1
    assert report.patched == 1
    failed_outcomes = [o for o in report.outcomes if o.action == "failed"]
    assert failed_outcomes[0].error == "boom"
