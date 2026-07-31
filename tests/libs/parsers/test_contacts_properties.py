"""Property tests for contact parsing helpers.

These tests exercise normalization invariants over unbounded provider input,
while keeping the known phone-ordering behavior isolated until its follow-up
bug is fixed.
"""

# ruff: noqa: PLR2004, S101, FBT001

from __future__ import annotations

import pytest
from hypothesis import (
    example,
    given,
    strategies as st,
)

from libs.parsers.contacts import (
    parse_first_middle_and_last_name,
    parse_name_case,
    parse_phone,
    parse_year,
)

# Hypothesis's @given is not typed in a way pyright's strict mode can see
# through.
# pyright: reportUntypedFunctionDecorator=false


@given(value=st.one_of(st.integers(), st.text()))
def test_parse_year_returns_none_or_a_year_at_least_1900(value: int | str) -> None:
    result = parse_year(value)
    negative = (
        isinstance(value, int) and not isinstance(value, bool) and value < 0
    ) or (isinstance(value, str) and value.strip().strip("'\"").startswith("-"))
    if negative:
        assert result is None
    else:
        assert result is None or result >= 1900


@pytest.mark.parametrize("value", [True, False])
def test_parse_year_rejects_booleans(value: bool) -> None:
    assert parse_year(value) is None


@given(name=st.text())
@example(name="Dr. Dr. Smith")
@example(name="Dr. Dr.")
def test_parse_name_case_is_idempotent(name: str) -> None:
    normalized = parse_name_case(name)
    assert parse_name_case(normalized) == normalized


@given(full_name=st.text())
def test_parse_first_middle_and_last_name_is_total_and_has_no_empty_fields(
    full_name: str,
) -> None:
    first_name, middle_name, last_name = parse_first_middle_and_last_name(full_name)
    assert all(
        part is None or part != "" for part in (first_name, middle_name, last_name)
    )


_VALID_PHONE_NUMBERS = (
    "+14155552671",
    "+442071838750",
    "+33142685300",
)


@pytest.mark.xfail(
    strict=True,
    reason="parse_phone currently returns arbitrary set iteration order",
)
@given(numbers=st.lists(st.sampled_from(_VALID_PHONE_NUMBERS), min_size=1, max_size=4))
@example(
    numbers=[
        "+14155552671",
        "+442071838750",
        "+33142685300",
    ],
)
def test_parse_phone_selects_first_valid_number(
    numbers: list[str],
) -> None:
    assert parse_phone(", ".join(numbers)) == numbers[0]
