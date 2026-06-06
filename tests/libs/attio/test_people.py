# pyright: reportPrivateUsage=false
"""Unit tests for libs/attio/people.py helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@patch("libs.attio.people._search_people_raw")
def test_get_person_values_email_match_uses_email_only(mock_search) -> None:
    """T6: matching_attribute=email selects on the email identity only, never
    OR-ing in linkedin/github_handle. Asserting the underlying search runs with
    only `email` populated captures the single-identity contract."""
    mock_search.return_value = []  # no match -> short-circuits before any GET

    from libs.attio.people import get_person_values

    get_person_values(
        matching_attribute="email",
        email="x@y.com",
        linkedin="https://linkedin.com/in/foo",
        github_handle="foo",
    )

    mock_search.assert_called_once()
    kwargs = mock_search.call_args.kwargs
    assert kwargs.get("email") == "x@y.com"
    assert kwargs.get("linkedin") is None
    assert kwargs.get("github_handle") is None


@patch("libs.attio.people.get_client")
@patch("libs.attio.people._search_people_raw")
def test_get_person_values_github_handle_match(mock_search, mock_get_client) -> None:
    """T7: a single github_handle match reads that record's values by id."""
    from libs.attio.models import PersonSearchResult
    from libs.attio.people import get_person_values

    mock_search.return_value = [PersonSearchResult(record_id="rec_1")]
    mock_client = MagicMock()
    get_by_id = mock_client.records.get_v2_objects_object_records_record_id_
    get_by_id.return_value.data.values = {"title": [{"value": "CTO"}]}
    mock_get_client.return_value.__enter__.return_value = mock_client

    result = get_person_values(
        matching_attribute="github_handle",
        github_handle="octocat",
    )

    assert mock_search.call_args.kwargs.get("github_handle") == "octocat"
    assert get_by_id.call_args.kwargs["record_id"] == "rec_1"
    assert result == {"title": [{"value": "CTO"}]}


@patch("libs.attio.people.get_client")
@patch("libs.attio.people._search_people_raw")
def test_get_person_values_github_handle_picks_canonical_on_multimatch(
    mock_search,
    mock_get_client,
) -> None:
    """A non-unique github_handle can match >1 record after a concurrent create.
    The read must select the SAME canonical record upsert_person writes to (the
    lexicographically-smallest record_id), then read THAT record's values — not
    whichever row Attio yields first — so merge_only_if_empty stays aligned with
    the write."""
    from libs.attio.models import PersonSearchResult
    from libs.attio.people import get_person_values

    # Returned out of canonical order on purpose.
    mock_search.return_value = [
        PersonSearchResult(record_id="rec_b"),
        PersonSearchResult(record_id="rec_a"),
    ]
    mock_client = MagicMock()
    get_by_id = mock_client.records.get_v2_objects_object_records_record_id_
    get_by_id.return_value.data.values = {"title": [{"value": "from A"}]}
    mock_get_client.return_value.__enter__.return_value = mock_client

    result = get_person_values(
        matching_attribute="github_handle",
        github_handle="octocat",
    )

    # Must read the canonical (smallest) record_id, not the first yielded.
    assert get_by_id.call_args.kwargs["record_id"] == "rec_a"
    assert result == {"title": [{"value": "from A"}]}


@patch("libs.attio.people.get_client")
def test_get_person_values_raises_when_required_identifier_missing(
    mock_get_client,
) -> None:
    """T8: ValueError when matching_attribute is set but the corresponding
    identifier is None. Attio client must not be invoked."""
    from libs.attio.people import get_person_values

    with pytest.raises(ValueError, match="github_handle"):
        get_person_values(
            matching_attribute="github_handle",
            email="x@y.com",
            linkedin="https://linkedin.com/in/foo",
            github_handle=None,
        )

    mock_get_client.assert_not_called()


@patch("libs.attio.people.get_client")
def test_get_person_values_linkedin_expands_url_variants(mock_get_client) -> None:
    """T9: When matching_attribute=linkedin, the read filter must expand to the
    same URL variants the write path searches across. Otherwise a non-canonical
    URL on the op can miss the existing record on the read side while the write
    still hits it — silently bypassing merge_only_if_empty protection.

    Regression test for the Codex review on the ai-805 push: raw equality on
    linkedin would leave a read/write mismatch for variant URLs.
    """
    mock_client = MagicMock()
    mock_client.records.post_v2_objects_object_records_query.return_value.data = []
    mock_get_client.return_value.__enter__.return_value = mock_client

    from libs.attio.people import _linkedin_url_variants, get_person_values

    input_url = "https://linkedin.com/in/foo"
    get_person_values(
        matching_attribute="linkedin",
        email=None,
        linkedin=input_url,
        github_handle=None,
    )

    call = mock_client.records.post_v2_objects_object_records_query.call_args
    filter_ = call.kwargs["filter_"]

    expected_variants = _linkedin_url_variants(input_url)
    assert len(expected_variants) > 1, (
        "test setup expects a non-canonical URL that expands to >1 variant"
    )
    assert filter_ == {
        "$or": [{"linkedin": v} for v in expected_variants],
    }, f"Expected $or-of-variants filter, got {filter_}"


@patch("libs.attio.people.get_client")
def test_update_person_missing_selector_is_validation_error(mock_get_client) -> None:
    """A missing id+email is a client-input error (→400), not a lookup miss
    (→404). Guards the AttioNotFoundError→AttioValidationError split (ai-h5y)."""
    from libs.attio.errors import AttioValidationError
    from libs.attio.models import PersonInput
    from libs.attio.people import update_person

    mock_get_client.return_value.__enter__.return_value = MagicMock()

    # The selector check is on the `email` argument, not the input payload —
    # the input still needs a valid identifier to construct.
    with pytest.raises(AttioValidationError):
        update_person(
            record_id=None,
            email=None,
            input=PersonInput(github_handle="elviskahoro"),
        )
