"""Property tests for libs/parsers/countries.py — free-text country → ISO alpha-2.

These complement the example table in ``test_countries.py`` rather than replacing
it: the table pins specific known provider spellings (``"USA"``, ``"UK"``) and
documents intent for a human reader.

A "property" here means a universally-quantified claim. How it is *checked*
depends on the domain, and the two cases are deliberately not treated alike:

* **Finite, enumerable domain** (the ~250 ISO countries) → checked
  **exhaustively**, with a plain loop. ``st.sampled_from`` would be weaker, not
  stronger: ``max_examples`` caps the draws well below 250, and because CI runs
  ``derandomize=True`` the same subset would be drawn on every run forever,
  leaving a fixed set of countries permanently untested.
* **Unbounded domain** (arbitrary provider text) → checked with Hypothesis,
  which is the only way to reach inputs nobody enumerated.

The totality property is the one that carries real weight. ``format_location``
requires alpha-2 by contract (see ai-sfp) and refuses to emit a location without
one, so ``country_name_to_iso2`` returning a *plausible but wrong* non-None value
would stamp the wrong country on a record instead of skipping the write — the
exact silent misattribution its docstring promises to avoid.
"""

from __future__ import annotations

import pycountry
from hypothesis import (
    event,
    given,
    strategies as st,
)

from libs.parsers.countries import country_name_to_iso2

# Hypothesis's @given is not typed in a way pyright's strict mode can see
# through, so every property test would otherwise need its own
# `# pyright: ignore[reportUntypedFunctionDecorator]`. Scope the suppression to
# the file instead — same pattern as the single ignore in tests/conftest.py, but
# it does not have to be repeated per test.
# pyright: reportUntypedFunctionDecorator=false


# Flattened to plain (str, str) pairs so the tests need no pycountry typing
# gymnastics — its record objects carry dynamic attributes.
_NAME_TO_ALPHA2: tuple[tuple[str, str], ...] = tuple(
    (country.name, country.alpha_2) for country in pycountry.countries
)
_ALPHA2_CODES = frozenset(alpha2 for _, alpha2 in _NAME_TO_ALPHA2)

# Padding the lookup is meant to be a no-op. "\t" is included because provider
# CSV exports carry tab-padded cells and ``str.strip()`` handles it — asserting
# that keeps the whitespace contract honest beyond plain spaces.
_WHITESPACE_PADDING = ("", " ", "  ", "\t")


def _pad_with_spaces(name: str) -> str:
    return f"  {name} "


def _drop_last_character(name: str) -> str:
    """Truncate by one character, e.g. "Chad" -> "Cha".

    A near-miss that must NOT resolve — the failure mode being guarded against is
    a lookup loose enough to match a neighbouring country on a partial name.
    """
    return name[:-1]


def _country_like_text() -> st.SearchStrategy[str]:
    """Text that plausibly reaches ``country_name_to_iso2`` in production.

    Bare ``st.text()`` is not enough on its own here. Random strings essentially
    never resolve to a country, so a property whose interesting branch only runs
    on a *successful* lookup would early-return on nearly every example and pass
    vacuously. Mixing real names, real codes and near-misses in alongside the
    garbage makes both branches reachable; the ``event()`` calls in the tests
    below report the actual split so vacuity stays visible in
    ``--hypothesis-show-statistics`` rather than being assumed away.
    """
    names = [name for name, _ in _NAME_TO_ALPHA2]
    return st.one_of(
        st.text(),
        st.sampled_from(names),
        st.sampled_from(sorted(_ALPHA2_CODES)),
        # Near-misses: the shapes providers actually send — case noise, padding,
        # and truncations that should NOT resolve to a neighbouring country.
        st.sampled_from(names).map(str.upper),
        st.sampled_from(names).map(_pad_with_spaces),
        st.sampled_from(names).map(_drop_last_character),
    )


def test_every_iso_country_name_resolves_to_its_own_alpha2() -> None:
    """Exhaustive over all ~250 ISO countries; the example table covers 6.

    Also guards ``_ALIASES`` against shadowing: an alias key that collides with
    an official ISO name but maps to a different code fails here. Because this is
    exhaustive rather than sampled, adding such an alias cannot slip through on a
    lucky seed.
    """
    mismatches = {
        name: (country_name_to_iso2(name), expected)
        for name, expected in _NAME_TO_ALPHA2
        if country_name_to_iso2(name) != expected
    }
    assert not mismatches, (
        f"names that did not resolve to their own alpha-2: {mismatches}"
    )


def test_lookup_ignores_case_and_surrounding_whitespace() -> None:
    """The case/whitespace contract, stated once instead of once per table row.

    Exhaustive over every (country, padding) pair for the same reason as above.
    """
    mismatches = {
        noisy_name: (country_name_to_iso2(noisy_name), expected)
        for name, expected in _NAME_TO_ALPHA2
        for padding in _WHITESPACE_PADDING
        if country_name_to_iso2(noisy_name := f"{padding}{name.upper()}{padding}")
        != expected
    }
    assert not mismatches, (
        f"case/whitespace variants that did not resolve: {mismatches}"
    )


@given(name=_country_like_text())
def test_never_raises_and_returns_none_or_a_real_alpha2(name: str) -> None:
    """Totality: the no-silent-default contract holds for ANY string.

    Unbounded domain, so this is where Hypothesis earns its place — it asserts
    something about every possible input rather than about a chosen few. A bare
    ``return`` from the function is fine; an exception, or a non-None value that
    is not a real alpha-2 code, is not.
    """
    result = country_name_to_iso2(name)
    event("resolved" if result is not None else "unresolved")
    assert result is None or result in _ALPHA2_CODES


@given(name=_country_like_text())
def test_resolved_codes_are_fixed_points(name: str) -> None:
    """Idempotence: feeding a resolved code back in returns the same code.

    Callers chain normalization (provider text → alpha-2 → ``format_location``),
    so a code that failed to round-trip would silently change country on a second
    pass.
    """
    result = country_name_to_iso2(name)
    if result is None:
        event("unresolved — fixed-point branch not exercised")
        return
    event("resolved — fixed-point branch exercised")
    assert country_name_to_iso2(result) == result
