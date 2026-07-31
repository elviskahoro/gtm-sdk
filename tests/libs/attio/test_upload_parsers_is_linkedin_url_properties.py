# ruff: noqa: INP001, S101

"""Property tests for the LinkedIn URL classifier.

The classifier is a security boundary: it must accept every normal spelling of
the LinkedIn host, while rejecting lookalikes and URL-parser bypasses.  The
input families below express that contract more broadly than a hand-maintained
table can, and the explicit examples preserve the regressions worth calling
out to a reader.
"""

from __future__ import annotations

import string

from hypothesis import (
    event,
    example,
    given,
    strategies as st,
)

from libs.attio.upload_parsers import is_linkedin_url

# Hypothesis's @given is not typed in a way pyright's strict mode can see
# through. Keep the suppression scoped to this property-test module.
# pyright: reportUntypedFunctionDecorator=false


def _subdomain(label: str) -> str:
    return f"{label}.linkedin.com"


def _alternate_case(value: str) -> str:
    return "".join(
        character.upper() if index % 2 else character
        for index, character in enumerate(value)
    )


def _valid_port(port: int) -> str:
    return f":{port}"


def _path_suffix(path: str) -> str:
    return f"/{path}" if path else ""


def _lookalike_prefix(label: str) -> str:
    return f"{label}-linkedin.com"


def _lookalike_suffix(label: str) -> str:
    return f"linkedin.com.{label}.tld"


def _contains_no_linkedin_host(value: str) -> bool:
    return "linkedin.com" not in value.casefold()


def _format_valid_url(
    host: str,
    prefix: str,
    port: str,
    path: str,
    padding: str,
) -> str:
    return f"{padding}{prefix}{host}{port}{path}{padding}"


def _with_lookalike_host(prefix: str, host: str, path: str) -> str:
    return f"{prefix}{host}{path}"


def _with_disallowed_scheme(scheme: str, path: str) -> str:
    return f"{scheme}://linkedin.com{path}"


def _with_userinfo(path: str) -> str:
    return f"https://user:secret@linkedin.com{path}"


def _with_malformed_port(prefix: str, port: str, path: str) -> str:
    return f"{prefix}linkedin.com:{port}{path}"


_HOST_LABEL: st.SearchStrategy[str] = st.from_regex(
    r"[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?",
    fullmatch=True,
)
_LINKEDIN_HOST = st.one_of(
    st.just("linkedin.com"),
    _HOST_LABEL.map(_subdomain),
)
_CASED_LINKEDIN_HOST = st.one_of(
    _LINKEDIN_HOST,
    _LINKEDIN_HOST.map(str.upper),
    _LINKEDIN_HOST.map(_alternate_case),
)
_WEB_PREFIXES = ("", "http://", "https://", "//")
_VALID_PORT = st.one_of(
    st.just(""),
    st.integers(min_value=1, max_value=65535).map(_valid_port),
)
_PATH = st.text(
    alphabet=string.ascii_letters + string.digits + "-._~/",
    max_size=40,
).map(_path_suffix)
_PADDING_VALUES = ("", " ", "  ", "\t")
_VALID_LINKEDIN_URL_PARTS = st.tuples(
    _CASED_LINKEDIN_HOST,
    _VALID_PORT,
    _PATH,
)


_HOST_CONFUSION = _HOST_LABEL.map(_lookalike_suffix)
_BRAND_LOOKALIKE = _HOST_LABEL.map(_lookalike_prefix)
_DISALLOWED_SCHEMES = ("ftp", "javascript", "file", "data", "mailto")
_MALFORMED_PORTS = ("notaport", "443.evil.tld", "65536")
_ABSENT_VALUES: tuple[tuple[str, str | None], ...] = (
    ("absent-value", None),
    ("absent-value", ""),
    ("absent-value", "   "),
)
_NON_LINKEDIN_TEXT = st.text(
    alphabet=string.ascii_letters + string.digits + " -_.",
).filter(_contains_no_linkedin_host)
_TAGGED_UNSAFE_VALUES: st.SearchStrategy[tuple[str, str]] = st.one_of(
    st.tuples(st.just("host"), _HOST_CONFUSION),
    st.tuples(st.just("lookalike"), _BRAND_LOOKALIKE),
    st.tuples(st.just("userinfo"), _PATH.map(_with_userinfo)),
    st.tuples(st.just("plain-text"), _NON_LINKEDIN_TEXT),
)


@given(parts=_VALID_LINKEDIN_URL_PARTS)
@example(parts=("LinkedIn.com", "", ""))
@example(parts=("www.linkedin.com", "", "/in/foo"))
@example(parts=("linkedin.com", ":443", "/in/foo"))
def test_is_linkedin_url_accepts_canonical_linkedin_hosts(
    parts: tuple[str, str, str],
) -> None:
    """Every supported URL spelling that resolves to LinkedIn is accepted."""
    host, port, path = parts
    for prefix in _WEB_PREFIXES:
        for padding in _PADDING_VALUES:
            event("canonical LinkedIn URL")
            value = _format_valid_url(host, prefix, port, path, padding)
            assert is_linkedin_url(value) is True


@given(case_and_value=_TAGGED_UNSAFE_VALUES)
@example(case_and_value=("lookalike", "evil-linkedin.com"))
@example(case_and_value=("host", "linkedin.com.attacker.tld"))
@example(case_and_value=("plain-text", "https://evil.com/linkedin.com"))
@example(case_and_value=("plain-text", "notlinkedin.com"))
@example(case_and_value=("plain-text", "linkedincom"))
@example(case_and_value=("plain-text", "not a url"))
@example(case_and_value=("userinfo", "foo@linkedin.com"))
@example(case_and_value=("userinfo", "https://foo@linkedin.com/in/bar"))
@example(case_and_value=("userinfo", "https://foo:bar@linkedin.com/in/baz"))
@example(case_and_value=("scheme", "mailto:foo@linkedin.com"))
def test_is_linkedin_url_rejects_unsafe_or_non_linkedin_values(
    case_and_value: tuple[str, str],
) -> None:
    """Lookalikes and parser bypasses never become trusted LinkedIn URLs."""
    case, value = case_and_value
    event(case)
    assert is_linkedin_url(value) is False


@given(host=_HOST_CONFUSION, path=_PATH)
@example(host="linkedin.com.attacker.tld", path="/in/foo")
def test_is_linkedin_url_rejects_prefixed_host_confusion(
    host: str,
    path: str,
) -> None:
    """A hostname extending linkedin.com is unsafe in every web URL form."""
    for prefix in _WEB_PREFIXES:
        event("host")
        assert is_linkedin_url(_with_lookalike_host(prefix, host, path)) is False


@given(host=_BRAND_LOOKALIKE, path=_PATH)
@example(host="evil-linkedin.com", path="/in/foo")
def test_is_linkedin_url_rejects_prefixed_brand_lookalikes(
    host: str,
    path: str,
) -> None:
    """Brand lookalikes are unsafe in every web URL form."""
    for prefix in _WEB_PREFIXES:
        event("lookalike")
        assert is_linkedin_url(_with_lookalike_host(prefix, host, path)) is False


@given(path=_PATH)
@example(path="/in/foo")
def test_is_linkedin_url_rejects_exhaustive_unsafe_url_forms(path: str) -> None:
    """Every finite unsafe scheme and malformed-port form remains rejected."""
    for scheme in _DISALLOWED_SCHEMES:
        event("scheme")
        assert is_linkedin_url(_with_disallowed_scheme(scheme, path)) is False
    for prefix in _WEB_PREFIXES:
        for port in _MALFORMED_PORTS:
            event("malformed-port")
            assert is_linkedin_url(_with_malformed_port(prefix, port, path)) is False


@given(tagged_values=st.just(_ABSENT_VALUES))
def test_is_linkedin_url_rejects_absent_values(
    tagged_values: tuple[tuple[str, str | None], ...],
) -> None:
    """Missing or whitespace-only source fields are never URLs."""
    for case, value in tagged_values:
        event(case)
        assert is_linkedin_url(value) is False
