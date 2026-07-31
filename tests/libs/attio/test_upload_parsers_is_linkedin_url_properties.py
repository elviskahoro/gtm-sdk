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
_WEB_PREFIX = st.sampled_from(("", "http://", "https://", "//"))
_VALID_PORT = st.one_of(
    st.just(""),
    st.integers(min_value=1, max_value=65535).map(_valid_port),
)
_PATH = st.text(
    alphabet=string.ascii_letters + string.digits + "-._~/",
    max_size=40,
).map(_path_suffix)
_PADDING = st.sampled_from(("", " ", "  ", "\t"))
_VALID_LINKEDIN_URLS = st.builds(
    _format_valid_url,
    _CASED_LINKEDIN_HOST,
    _WEB_PREFIX,
    _VALID_PORT,
    _PATH,
    _PADDING,
)


_LOOKALIKE_HOST = st.one_of(
    _HOST_LABEL.map(_lookalike_prefix),
    _HOST_LABEL.map(_lookalike_suffix),
)
_DISALLOWED_SCHEME = st.sampled_from(
    ("ftp", "javascript", "file", "data", "mailto"),
)
_MALFORMED_PORT = st.sampled_from(("notaport", "443.evil.tld", "65536"))
_NON_LINKEDIN_TEXT = st.text(
    alphabet=string.ascii_letters + string.digits + " -_.",
).filter(_contains_no_linkedin_host)
_UNSAFE_OR_NON_LINKEDIN_VALUES = st.one_of(
    st.builds(_with_lookalike_host, _WEB_PREFIX, _LOOKALIKE_HOST, _PATH),
    st.builds(_with_disallowed_scheme, _DISALLOWED_SCHEME, _PATH),
    st.builds(_with_userinfo, _PATH),
    st.builds(_with_malformed_port, _WEB_PREFIX, _MALFORMED_PORT, _PATH),
    _NON_LINKEDIN_TEXT,
)


@given(value=_VALID_LINKEDIN_URLS)
@example(value="LinkedIn.com")
@example(value="//linkedin.com/in/foo")
@example(value="//www.linkedin.com/in/foo")
@example(value="linkedin.com:443/in/foo")
@example(value="https://linkedin.com:443/in/foo")
def test_is_linkedin_url_accepts_canonical_linkedin_hosts(value: str) -> None:
    """Every supported URL spelling that resolves to LinkedIn is accepted."""
    event("canonical LinkedIn URL")
    assert is_linkedin_url(value) is True


@given(value=_UNSAFE_OR_NON_LINKEDIN_VALUES)
@example(value="evil-linkedin.com")
@example(value="linkedin.com.attacker.tld")
@example(value="https://evil.com/linkedin.com")
@example(value="notlinkedin.com")
@example(value="linkedincom")
@example(value="not a url")
@example(value="ftp://linkedin.com/in/foo")
@example(value="javascript://linkedin.com/in/foo")
@example(value="file://linkedin.com/in/foo")
@example(value="data://linkedin.com/in/foo")
@example(value="mailto:foo@linkedin.com")
@example(value="foo@linkedin.com")
@example(value="https://foo@linkedin.com/in/bar")
@example(value="https://foo:bar@linkedin.com/in/baz")
@example(value="linkedin.com:443.evil.tld")
@example(value="linkedin.com:notaport/in/foo")
@example(value="https://linkedin.com:443.evil.tld/in/foo")
def test_is_linkedin_url_rejects_unsafe_or_non_linkedin_values(value: str) -> None:
    """Lookalikes and parser bypasses never become trusted LinkedIn URLs."""
    event("unsafe or non-LinkedIn input")
    assert is_linkedin_url(value) is False


@example(value=None)
@example(value="")
@example(value="   ")
@given(value=st.one_of(st.none(), st.just(""), st.just("   ")))
def test_is_linkedin_url_rejects_absent_values(value: str | None) -> None:
    """Missing or whitespace-only source fields are never URLs."""
    assert is_linkedin_url(value) is False
