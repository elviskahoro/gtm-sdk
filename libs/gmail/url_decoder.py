"""Decode Gmail web UI URL fragments to Gmail API message/thread IDs.

Gmail's web client encodes thread/message IDs using a custom base-40 alphabet
(all consonants, no vowels). The decoded payload is a UTF-8 string like
``f:1862404484635518038`` where the number is the decimal form of the hex API ID.

Reference: https://github.com/ArsenalRecon/GmailURLDecoder
"""

from __future__ import annotations

import base64
import re
from urllib.parse import urlparse

_GMAIL_CHARSET = "BCDFGHJKLMNPQRSTVWXZbcdfghjklmnpqrstvwxz"
_B64_CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

_FMFCG_RE = re.compile(r"[A-Za-z]{20,}")
_HEX_ID_RE = re.compile(r"^[0-9a-f]{10,16}$")
_PREFIXES = ("thread-f:", "msg-f:", "f:")


def _base_convert(token: str, from_charset: str, to_charset: str) -> str:
    from_base = len(from_charset)
    to_base = len(to_charset)
    value = 0
    for ch in token:
        value = value * from_base + from_charset.index(ch)
    result: list[str] = []
    while value > 0:
        result.append(to_charset[value % to_base])
        value //= to_base
    return "".join(reversed(result))


def decode_token(token: str) -> str | None:
    """Decode a Gmail URL token (``FMfcg...``) to a hex API ID.

    Returns the hex thread/message ID usable with the Gmail API, or None if
    the token cannot be decoded.
    """
    if _HEX_ID_RE.match(token):
        return token

    try:
        b64 = _base_convert(token, _GMAIL_CHARSET, _B64_CHARSET)
        b64 += "=" * (-len(b64) % 4)
        raw = base64.b64decode(b64).decode("utf-8", errors="replace")
    except (ValueError, KeyError):
        return None

    for prefix in _PREFIXES:
        if prefix in raw:
            num_str = raw.split(prefix, 1)[1].strip().rstrip("\x00")
            try:
                return format(int(num_str), "x")
            except ValueError:
                return None
    return None


def extract_id_from_url(url: str) -> str | None:
    """Extract and decode a Gmail API ID from a full Gmail web URL.

    Accepts URLs like:
        https://mail.google.com/mail/u/0/#inbox/FMfcgzQgLPNbWJPPscDGRbsGngFwlCpq
        https://mail.google.com/mail/u/0/#inbox/19d89702e6cb6456
    """
    parsed = urlparse(url)
    fragment = parsed.fragment
    if not fragment:
        return None

    parts = fragment.rstrip("/").split("/")
    token = parts[-1] if parts else None
    if not token:
        return None

    return decode_token(token)
