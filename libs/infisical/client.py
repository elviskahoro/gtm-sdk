"""Thin wrapper around the Infisical SDK for runtime secret fetch.

Resolution order for each name: session env var first, then Infisical fetch.
The PyPI package is ``infisicalsdk``; the import path is ``infisical_sdk``.

Bootstrap creds expected in os.environ (set by ``modal.Secret.from_dict(...)``
on the function, populated from the deploy-time shell environment):

- ``INFISICAL_TOKEN`` — service or machine-identity access token
- ``INFISICAL_PROJECT_ID`` — Infisical project ID
- ``INFISICAL_ENV`` — environment slug (``dev``/``staging``/``prod``). REQUIRED;
  no default — see ai-2aw. A silent default would resurrect the exact
  miss-route footgun this module was built to eliminate (operator forgets
  to set the env, prod traffic silently lands in dev Attio).
- ``INFISICAL_HOST`` (optional) — defaults to ``https://app.infisical.com``
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import ExitStack, contextmanager

from libs.infisical.errors import InfisicalAuthError, InfisicalFetchError

_DEFAULT_HOST = "https://app.infisical.com"
# Infisical organizes secrets in a virtual filesystem rooted at "/"; this is
# the project-root path, not a password. (bandit false-positive on B105.)
_DEFAULT_SECRET_PATH = "/"  # nosec B105 # noqa: S105


def _login_client():
    from infisical_sdk import InfisicalSDKClient

    token = os.environ.get("INFISICAL_TOKEN", "").strip()
    project_id = os.environ.get("INFISICAL_PROJECT_ID", "").strip()
    if not token or not project_id:
        raise InfisicalAuthError(
            "INFISICAL_TOKEN and INFISICAL_PROJECT_ID must be set to fetch from Infisical.",
        )
    host = os.environ.get("INFISICAL_HOST", "").strip() or _DEFAULT_HOST
    return InfisicalSDKClient(host=host, token=token)


def _fetch_from_infisical(name: str) -> str:
    client = _login_client()
    env_slug = os.environ.get("INFISICAL_ENV", "").strip()
    if not env_slug:
        # Fail closed instead of defaulting to "dev". A silent default would
        # mean an operator who forgets to ``export INFISICAL_ENV=prod`` on a
        # webhook deploy lands prod traffic in the dev Attio workspace — the
        # exact miss-route shape ai-2aw was filed to eliminate.
        raise InfisicalAuthError(
            "INFISICAL_ENV must be set explicitly (dev|staging|prod). "
            "No default is applied — see ai-2aw. For webhook deploys, "
            "scripts/deploy-webhook.sh preflights this; for direct callers, "
            "export INFISICAL_ENV before invoking modal deploy or modal serve.",
        )
    try:
        secret = client.secrets.get_secret_by_name(
            secret_name=name,
            project_id=os.environ["INFISICAL_PROJECT_ID"],
            environment_slug=env_slug,
            secret_path=_DEFAULT_SECRET_PATH,
        )
    except Exception as exc:
        raise InfisicalFetchError(
            f"Failed to fetch {name} from Infisical: {type(exc).__name__}: {exc}",
        ) from exc
    return (secret.secretValue or "").strip()


@contextmanager
def fetch(name: str) -> Generator[str, None, None]:
    """Yield the value of ``name`` from session env, falling back to Infisical.

    The ``with`` form is API ergonomics: callers read as "I am acquiring this
    secret for the duration of this block". There is no cleanup today; if we
    ever need to scrub the secret from memory after use, it goes here.
    """
    val = os.environ.get(name, "").strip()
    if val:
        yield val
        return
    val = _fetch_from_infisical(name)
    if not val:
        raise InfisicalFetchError(
            f"{name} resolved to an empty value (env miss; Infisical returned empty).",
        )
    yield val


@contextmanager
def fetch_all(names: list[str]) -> Generator[dict[str, str], None, None]:
    """Yield a ``{name: value}`` map, resolving each via :func:`fetch`."""
    with ExitStack() as stack:
        yield {name: stack.enter_context(fetch(name)) for name in names}
