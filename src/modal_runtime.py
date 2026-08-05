import subprocess
import time
from datetime import UTC, datetime

import modal

from libs.logging.structured import set_source
from libs.telemetry import init_log_exporter
from src.modal_app import MODAL_APP

# Force deployment with timestamp
_deploy_ts = time.time()
app = modal.App(name=MODAL_APP)

# Ship structured log events emitted from ``src/*`` (e.g.
# ``src/attio/export.py`` and ``src/enrichment.py``) to any OTLP-compatible
# sink. The OTEL env vars reach the container via
# ``src.secrets_bootstrap.bootstrap_secret`` (post ai-672), which now folds
# the OTLP-routing env vars into the inline Modal Secret alongside the
# Infisical creds. No-op when the OTEL env vars are unset.
#
# ``set_source`` binds the per-request lookup key that
# ``libs.logging.structured.log()`` uses to find the OTLP logger registered
# by ``init_log_exporter`` — both must agree on ``MODAL_APP`` so the
# strict-lookup path in ``libs.telemetry.get_otlp_logger`` resolves
# correctly.
set_source(MODAL_APP)
init_log_exporter(MODAL_APP)


def _resolve_git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["/usr/bin/git", "rev-parse", "--short", "HEAD"],
            text=True,
        )
        return out.strip() or "unknown"
    except Exception:  # noqa: BLE001 - git metadata fallback must never block import.
        return "unknown"


# HTTP endpoints with object-first naming (company_*, person_*, note_*)
# Deployment: 2026-03-29
image = (
    modal.Image.debian_slim(python_version="3.13")
    .pip_install(
        "attio>=0.21.2",
        "exa-py>=1.0",
        "fastapi[standard]",
        "flatsplode>=0.2.0",
        "gcsfs>=2024.10.0",
        "gtm-apollo>=0.0.2",
        "gtm-linear>=0.0.2",
        "infisicalsdk>=1.0.16",
        "orjson>=3.10.0",
        "parallel-web",
        "pydantic>=2.0",
        # Provides the `uuid_extensions` module imported by libs/logging/structured.py
        # (pulled in at src/app.py import time). Missing it crash-loops every
        # container on startup — masked until now because deploys were broken. (ai-8k7)
        "uuid7>=0.1.0",
    )
    .env(
        {
            "AI_BUILD_GIT_SHA": _resolve_git_sha(),
            "AI_DEPLOYED_AT": datetime.now(UTC).isoformat(),
        },
    )
    .add_local_python_source("libs")
    .add_local_python_source("src")
)
