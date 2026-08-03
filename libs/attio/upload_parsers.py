from urllib.parse import urlparse


def is_linkedin_url(
    domain: str | None,
) -> bool:
    """Return True iff ``domain`` points at the linkedin.com host over http(s).

    Accepts bare hosts (``linkedin.com/in/foo``), http/https URLs
    (``https://www.linkedin.com/...``), and mixed case. Rejects:

    - host lookalikes (``evil-linkedin.com``, ``linkedin.com.attacker.tld``);
    - non-web schemes (``ftp://``, ``javascript://``, ``mailto:``, ``file://``,
      ``data://``) even when the embedded host string is linkedin.com;
    - userinfo-style inputs (``foo@linkedin.com`` or
      ``https://foo@linkedin.com/...``), which urlparse would otherwise
      happily resolve to a linkedin.com hostname despite being email
      addresses or phishing-shaped URLs.

    Implementation note: the leading-dot suffix check on ``.linkedin.com`` is
    what closes CodeQL ``py/incomplete-url-substring-sanitization`` — a bare
    ``"linkedin.com" in host`` or ``host.startswith("linkedin.com")`` shape is
    bypassable and will be flagged again. The scheme allowlist and userinfo
    rejection close adjacent bypasses (scheme confusion and embedded
    userinfo) in the same check.
    """
    if not domain:
        return False

    raw: str = str(domain).strip().lower()
    if not raw:
        return False

    # urlparse needs either a scheme (`http://...`) or a leading `//`
    # (protocol-relative) to populate `netloc`/`hostname`. Normalize before
    # parsing so we don't (a) re-prepend `//` onto an already protocol-
    # relative URL and double-`//` the netloc, or (b) let bare-host inputs
    # with a port (``linkedin.com:443/in/foo``) get parsed as
    # ``scheme=linkedin.com``.
    if raw.startswith("//") or "://" in raw:
        candidate: str = raw
    else:
        candidate = f"//{raw}"

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False

    # Restrict to bare-host (no scheme) or explicit http(s). This rejects
    # ftp/javascript/file/data/mailto and any other non-web scheme that
    # happens to embed "linkedin.com".
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return False

    # Reject userinfo-style inputs. `foo@linkedin.com` is an email address,
    # not a LinkedIn URL — urlparse would otherwise return linkedin.com as
    # the hostname.
    if parsed.username or parsed.password:
        return False

    # Reject malformed authorities like `linkedin.com:443.evil.tld`, where
    # urlparse will happily return `hostname == "linkedin.com"` and a
    # non-numeric port. Accessing `parsed.port` validates the port and
    # raises ValueError on anything that isn't a numeric port in range.
    try:
        _ = parsed.port
    except ValueError:
        return False

    host: str | None = parsed.hostname
    if not host:
        return False

    return host == "linkedin.com" or host.endswith(".linkedin.com")
