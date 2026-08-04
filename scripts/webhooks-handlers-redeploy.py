#!/usr/bin/env python3
# trunk-ignore-all(bandit/B607): list-arg subprocess only; tool names resolved via PATH on purpose.
"""Substitute, deploy, and restore a webhook handler in one safe step.

Port of scripts/redeploy-webhook.sh. Host-side Python does discovery,
preflights, atomic locking, backup, placeholder substitution, restore, and
restore verification. The deploy itself is one recipe run by one of two
executors, so the env that ships images to Modal is reproducible
operator-to-operator.

Every footgun this script exists to prevent is documented on the function
that encodes it, as an explicit preflight or cleanup step -- this module is
the catalogue, so add new rationale here rather than to a rules file that
cannot be kept in sync. The CI smoke test at ``tests/scripts/test_deploy_webhook.py``
exercises the substitute/restore loop, the EXIT-equivalent restore on deploy
failure, the Modal-token pop at the preflight, and the environment scrub.

Usage:
    scripts/webhooks-handlers-redeploy.py <handler> <source>
    scripts/webhooks-handlers-redeploy.py <handler> --all

Both transports run the *same* recipe -- see ``deploy_steps`` / ``deploy_env``
below. Flox is the default; ``RUN_WITH_DAGGER=1`` opts into the shared
prebuilt Flox-toolchain container for isolation. The test suite uses the
primary path so CI needs no Dagger engine.

The shebang is a plain ``python3``, not ``uv run python``: ``[tool.uv]
required-version`` in pyproject.toml makes *any* incompatible ``uv`` binary
refuse to run before Python even starts, so a ``uv run python`` shebang can't
survive a pyenv shim shadowing a compatible Homebrew/Flox install ahead of it
on PATH. ``_bootstrap_uv()`` below resolves a version-compatible ``uv`` via
``scripts/lib/uv_resolve.py`` (which scans *all* of PATH, not just the first
match) and re-execs into it. This does **not** protect an explicitly-typed
``uv run scripts/webhooks-handlers-redeploy.py ...`` against an already
incompatible shell ``uv`` — that binary refuses before any of our code runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))
from scripts.lib.container import (  # noqa: E402
    RUN_WITH_DAGGER,
    in_container_phase,
    run_recipe_in_container_async,
)
from scripts.lib.env import env_flag  # noqa: E402
from scripts.lib.flox import (  # noqa: E402
    FloxEnvironmentNotActivatedError,
    preflight as flox_preflight,
    run as flox_run,
)
from scripts.lib.uv_resolve import (  # noqa: E402
    NoCompatibleUvError,
    find_compatible_uv_for_repo,
)


def _fail(msg: str) -> NoReturn:
    """Print a user-facing preflight error and terminate the command."""
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


_UV_BOOTSTRAP_ENV = "_GTM_UV_BOOTSTRAPPED"


def _bootstrap_uv() -> None:
    """Re-exec into a version-compatible ``uv`` before any third-party import.

    Required for correctness, not just direct execution: two independent
    guards must both hold, or existing tests break in different ways.

    - ``if __name__ == "__main__":`` gates the call below.
      ``tests/scripts/test_deploy_webhook_dagger.py`` and
      ``test_deploy_webhook_dagger_real.py`` load this module via
      ``importlib.util.spec_from_file_location`` for white-box testing,
      under a synthetic module name -- never ``"__main__"``. Without this
      gate, merely importing the module for those tests would trigger
      resolve+``os.execv``, and since ``execv`` replaces the current
      process image, it would replace the entire pytest process.
    - The active-virtualenv check below skips re-exec when already running
      under any virtual environment. The caller has already selected a
      concrete interpreter with its dependencies; later subprocess calls
      still resolve a version-compatible uv explicitly.
      ``tests/scripts/test_deploy_webhook.py`` deliberately invokes this
      script via ``subprocess.run([sys.executable, str(SCRIPT), *args])``
      (not through the shebang) so its PATH-stubbed ``uv`` intercepts only
      the script's *internal* calls -- verified empirically that
      ``sys.executable`` there already *is* ``REPO_ROOT/.venv``'s
      interpreter (the suite runs via ``uv run pytest``), so this check
      makes the bootstrap correctly inert for that harness with no
      test-specific special-casing.
    """
    if os.environ.get(_UV_BOOTSTRAP_ENV):
        return
    if sys.prefix != sys.base_prefix:
        os.environ[_UV_BOOTSTRAP_ENV] = "1"
        return
    try:
        candidate = find_compatible_uv_for_repo(cwd=str(REPO_ROOT))
    except NoCompatibleUvError as exc:
        _fail(str(exc))
    os.environ[_UV_BOOTSTRAP_ENV] = "1"
    script_path = str(Path(__file__).resolve())
    # Unlike subprocess.run(cwd=...), os.execv has no cwd parameter -- the
    # re-exec'd process always inherits whatever cwd this process actually
    # has right now. The probe above used cwd=REPO_ROOT (a pyenv shim
    # resolves a different real binary depending on directory), so without
    # actually chdir'ing here first, a script invoked from some other
    # directory could dispatch through the shim differently once re-exec'd
    # than what was just verified compatible.
    os.chdir(REPO_ROOT)
    # execv replaces the process image in place (same PID) -- exit codes and
    # signals propagate for free, no wrapper process needed. Use the literal
    # "python", not sys.executable, which pre-bootstrap is the wrong, ambient
    # interpreter. --project pins project discovery to REPO_ROOT regardless
    # of the caller's cwd -- without it, invoking this script via an absolute
    # path from outside the repo makes `uv run` resolve the wrong (or no)
    # project and `import dagger` fails with ModuleNotFoundError.
    # trunk-ignore(bandit/B606): argv is the resolved uv binary + this script's own path
    os.execv(  # noqa: S606
        candidate.path,
        [
            candidate.path,
            "run",
            "--project",
            str(REPO_ROOT),
            "python",
            script_path,
            *sys.argv[1:],
        ],
    )
    msg = "os.execv() returned unexpectedly"
    raise AssertionError(msg)  # pragma: no cover


if __name__ == "__main__":
    _bootstrap_uv()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import atexit  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
from typing import TYPE_CHECKING, NamedTuple, NoReturn  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterable

WEBHOOKS_DIR = REPO_ROOT / "webhooks"
BACKUP_DIR = REPO_ROOT / "tmp" / "webhook-deploy-bak"
LOCK_DIR = REPO_ROOT / "tmp" / "webhook-deploy.lock"

HANDLER_ALIASES: dict[str, str] = {
    "attio": "export_to_attio",
    "etl": "export_to_gcp_etl",
    "raw": "export_to_gcp_raw",
    "slack": "export_to_slack",
}

PLACEHOLDER = "WebhookModelToReplace"
REQUIRED_MODAL_SECRETS: tuple[str, ...] = ("devx-gcp-202605260000",)
VALID_INFISICAL_ENVS: tuple[str, ...] = ("dev", "staging", "prod")

# Module-level state read by ``_cleanup`` (registered via ``atexit`` and via
# SIGINT/SIGTERM handlers). Mirrors the bash trap that captured globals by
# name — until ``_backup_freshly_written`` flips to True, the cleanup hook is
# a no-op so an early-failure path cannot restore stale content over a clean
# worktree.
_handler: str | None = None
_handler_file: Path | None = None
_lock_acquired = False
_backup_freshly_written = False

# Populated once by _preflight_uv_version(); read by every internal `uv`
# subprocess call below so a child's own PATH lookup can't re-discover an
# incompatible shim even though this process is already running under a good
# one (the bootstrap re-exec only guarantees *this* process is compatible).
_uv_path: str | None = None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _discover_handlers() -> list[str]:
    """Return every ``webhooks/*.py`` basename that contains the placeholder.

    New handlers adopt the placeholder pattern by including it in their
    source; no edit to this script is required when one appears.
    """
    handlers = sorted(
        path.stem
        for path in WEBHOOKS_DIR.glob("*.py")
        if PLACEHOLDER in path.read_text()
    )
    if not handlers:
        _fail(
            f"No webhook handlers under {WEBHOOKS_DIR.relative_to(REPO_ROOT)} "
            f"contain {PLACEHOLDER}.",
        )
    return handlers


_SOURCE_RE = re.compile(
    r"^\s*Webhook as ([A-Za-z_][A-Za-z0-9_]*),?\s*$",
    re.MULTILINE,
)


def _discover_sources(handler_file: Path) -> list[str]:
    """Parse a handler's ``Webhook as <Alias>`` import lines.

    The imports are the single source of truth. Adding/removing a source in
    the handler propagates automatically on the next invocation.
    """
    text = handler_file.read_text()
    sources = _SOURCE_RE.findall(text)
    if not sources:
        _fail(f"No 'Webhook as <Alias>' imports found in {handler_file}.")
    return sources


def _source_module_for(handler_file: Path, source: str) -> str:
    """Resolve ``source`` to the dotted module path it was imported from.

    Walks the handler text, tracks the most recent ``from src.… import (``
    line, and emits its dotted form when the matching ``Webhook as <source>``
    appears. Mirrors the bash ``awk`` block.
    """
    last_from: str | None = None
    target = f"Webhook as {source}"
    for raw_line in handler_file.read_text().splitlines():
        if re.match(r"^from src\.[A-Za-z0-9_.]+ import \(", raw_line):
            last_from = raw_line
            continue
        if target in raw_line and last_from is not None:
            stripped = re.sub(r"^from ", "", last_from)
            return re.sub(r" import \(.*$", "", stripped)
    _fail(f"Could not resolve module path for {source} in {handler_file}.")


# ---------------------------------------------------------------------------
# Preflights (all host-side)
# ---------------------------------------------------------------------------


def _preflight_uv_version() -> None:
    """Resolve a version-compatible `uv` once; every later preflight needs it.

    `_bootstrap_uv()` (top of file) already established that *this process*
    is running under a compatible `uv`, but that doesn't guarantee a fresh
    child subprocess's own PATH lookup for a bare "uv" will find the same
    one -- this preflight resolves again (cheap; also serves as
    defense-in-depth reporting) and caches the absolute path in `_uv_path`
    for every internal `uv` subprocess call below to use instead of the
    bare string.
    """
    global _uv_path  # noqa: PLW0603 — module state read by later preflights
    try:
        candidate = find_compatible_uv_for_repo(cwd=str(REPO_ROOT))
    except NoCompatibleUvError as exc:
        _fail(str(exc))
    if candidate.version is None:  # defensive: find_compatible_uv only returns a match
        _fail(f"Resolved uv candidate has no parseable version: {candidate.path}")
    version_text = ".".join(map(str, candidate.version))
    print(f"Preflighting uv version: {candidate.path} (uv {version_text}) ✓")
    _uv_path = candidate.path


def _require_uv_path() -> str:
    """Return the compatible uv selected during bootstrap."""
    if _uv_path is None:  # set by _preflight_uv_version() before use
        _fail("uv path requested before uv preflight completed")
    return _uv_path


def _preflight_env() -> None:
    """Require Infisical bootstrap creds + an explicit INFISICAL_ENV.

    ``INFISICAL_ENV`` is the slug each deployed function uses at request time
    to fetch its per-domain API keys via ``libs.infisical.fetch_all``. We
    require it explicitly here so an operator who forgets to export it does
    not silently land prod webhook traffic in the dev Attio workspace — the
    exact miss-route shape ai-2aw was filed to eliminate. ``libs.infisical``
    also fails closed at runtime; this catches it before image build.

    Also pops the operator's personal ``MODAL_TOKEN_*``. Contrary to what
    this file and AGENTS.md used to claim, ``infisical run`` injection *wins*
    over the parent shell, so the pop is not what protects the deploy — the
    deploy takes its tokens from an explicit ``_fetch_infisical_value``. The
    pop still matters for ``_preflight_modal_secrets()``, which shells out
    through ``_infisical_run`` without that guarantee on every CLI version: a
    leaked personal token there would list the *wrong workspace's* secrets
    and pass a preflight for a workspace we are not deploying to.
    """
    for key in ("INFISICAL_PROJECT_ID", "INFISICAL_TOKEN"):
        if not os.environ.get(key):
            _fail(
                f"{key} is unset. Run: set -a && source .env.local && set +a",
            )
    env_slug = os.environ.get("INFISICAL_ENV", "")
    if not env_slug:
        _fail(
            "INFISICAL_ENV is unset. Export it explicitly before deploying "
            "(e.g. 'export INFISICAL_ENV=prod'). No default is applied — see "
            "ai-2aw.",
        )
    if env_slug not in VALID_INFISICAL_ENVS:
        _fail(
            f"INFISICAL_ENV='{env_slug}' not in "
            f"{{{','.join(VALID_INFISICAL_ENVS)}}}. Set one of those before "
            f"deploying.",
        )

    os.environ.pop("MODAL_TOKEN_ID", None)
    os.environ.pop("MODAL_TOKEN_SECRET", None)


def _preflight_working_tree() -> None:
    """Refuse to start if anything in ``webhooks/`` differs from HEAD.

    ``git diff --quiet`` only compares worktree against index — a
    staged-but-uncommitted edit would slip past. ``git status --porcelain``
    flags any deviation (staged, unstaged, or untracked).
    """
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "webhooks/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if status.strip():
        print(
            "ERROR: webhooks/ has uncommitted changes (staged, unstaged, or "
            "untracked). Aborting.",
            file=sys.stderr,
        )
        print(status, file=sys.stderr)
        sys.exit(1)


def _preflight_modal_secrets() -> None:
    """Verify every named Modal secret exists in the active workspace.

    Worth doing up front: a missing ``modal.Secret.from_name(...)`` does not
    surface until *after* the image build, so without this the operator pays
    a full build before learning the deploy cannot succeed.

    Uses ``--json`` because the default table renderer truncates long names
    like ``devx-gcp-202605260000`` with ``…``, which would make a substring
    grep miss the secret even when it exists. Matches the full
    ``"Name": "<secret>"`` token to keep a prefix like ``devx-gcp`` from
    accidentally satisfying a longer name.
    """
    print(f"Preflighting Modal secrets ({len(REQUIRED_MODAL_SECRETS)} required)")
    proc = _infisical_run(
        [_require_uv_path(), "run", "modal", "secret", "list", "--json"],
        env_slug="dev",
        capture_output=True,
    )
    if proc.returncode != 0:
        _fail(
            "Could not list Modal secrets — check Infisical token and Modal access.",
        )
    payload = proc.stdout
    for secret in REQUIRED_MODAL_SECRETS:
        needle = f'"Name": "{secret}"'
        if needle not in payload:
            _fail(
                "Missing one or more required Modal secrets in the dlthub "
                "workspace. Create required secrets before deploying.",
            )


# Keys the Slack handler needs but that are NOT declared on any source's
# required/optional_api_keys() — doing so would gate the Attio/GCS deploys on
# Slack secrets they never use. SLACK_BOT_TOKEN is shared across Slack sources;
# the target channel is per-source (each Webhook declares its own key via
# slack_get_channel_secret_name(), e.g. CALCOM_SLACK_CHANNEL_ID), so it's
# resolved per source in the preflight below rather than hardcoded here. This
# keeps the deploy-time existence check scoped to export_to_slack only (mirrors
# the handler-scoped GCS bucket preflight).
_SLACK_HANDLER_SHARED_API_KEYS: tuple[str, ...] = ("SLACK_BOT_TOKEN",)


def _preflight_infisical_keys(
    handler_file: Path,
    sources: Iterable[str],
) -> None:
    """Restore the deploy-time fail-fast for per-source Infisical API keys.

    ai-2aw moved ATTIO_API_KEY/CALCOM_API_KEY off named Modal Secrets and
    onto request-time ``libs.infisical.fetch_all``. That removed the
    deploy-time check that the secret existed; a typo or missing key in the
    target ``INFISICAL_ENV`` now ships cleanly and fails on the first
    Hookdeck event. Each ``Webhook`` subclass declares its keys via two
    static methods:

    - ``required_api_keys()`` — keys every event path on the source needs.
    - ``optional_api_keys()`` — keys reached lazily on only a subset of
      event types (e.g. ``CALCOM_API_KEY`` is only touched by caldotcom's
      ``BOOKING_NO_SHOW_UPDATED`` branch, so declaring it required would
      force the other Cal.com event types to fail-fast on a missing or
      rotated key they never use).

    We preflight the **union** so a missing/rotated key surfaces at deploy
    time instead of on the first qualifying Hookdeck event. Each key is
    verified with a separate ``infisical secrets get`` so the error names
    the specific missing secret. (See ai-q9k.)

    Important: ``infisical secrets get`` (CLI 0.43.84 against
    dlthub-sandbox/dev, confirmed 2026-05-26) exits **0 for both present
    and missing keys** and only differentiates via stdout — present keys
    print the value, missing keys print nothing. A pure ``returncode``
    check is therefore theater: the loop would always pass. We treat
    empty stdout (after strip) as 'missing' to match the only signal the
    CLI actually exposes. ``_fetch_infisical_value`` below uses the same
    pattern for MODAL_TOKEN_*. Do not "simplify" this back to a returncode
    check. (See ai-4pw.)
    """
    env_slug = os.environ["INFISICAL_ENV"]
    is_slack = handler_file.stem == "export_to_slack"
    is_clay = handler_file.stem == "export_to_clay"
    preflight: list[str] = []
    for source in sources:
        module = _source_module_for(handler_file, source)
        # For the Slack handler also emit the source's per-automation channel
        # key (slack_get_channel_secret_name(), e.g. CALCOM_SLACK_CHANNEL_ID) so
        # it's preflighted alongside the source's declared keys.
        if is_clay:
            keys_program = (
                "print(Webhook.clay_get_webhook_url_secret_name())\n"
                "print(Webhook.clay_get_webhook_auth_token_secret_name())\n"
            )
        else:
            extra = (
                "print(Webhook.slack_get_channel_secret_name())\n" if is_slack else ""
            )
            keys_program = (
                "for k in list(Webhook.required_api_keys()) + "
                "list(Webhook.optional_api_keys()):\n"
                "    print(k)\n"
                f"{extra}"
            )
        keys_text = subprocess.run(
            [
                _require_uv_path(),
                "run",
                "python",
                "-c",
                (f"from {module} import Webhook\n{keys_program}"),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if keys_text.returncode != 0:
            _fail(
                f"Could not resolve required_api_keys()/optional_api_keys() "
                f"for {source} via {module}.Webhook.",
            )
        for key in keys_text.stdout.splitlines():
            stripped = key.strip()
            if stripped and stripped not in preflight:
                preflight.append(stripped)

    # Handler-scoped shared keys: SLACK_BOT_TOKEN isn't on any source's
    # required/optional_api_keys() (see _SLACK_HANDLER_SHARED_API_KEYS), so add it
    # only when deploying export_to_slack.
    if is_slack:
        for key in _SLACK_HANDLER_SHARED_API_KEYS:
            if key not in preflight:
                preflight.append(key)

    if not preflight:
        return

    print(
        f"Preflighting Infisical keys in env={env_slug}: {' '.join(preflight)}",
    )
    for key in preflight:
        proc = subprocess.run(
            [
                "infisical",
                "secrets",
                "get",
                key,
                "--projectId",
                os.environ["INFISICAL_PROJECT_ID"],
                "--token",
                os.environ["INFISICAL_TOKEN"],
                f"--env={env_slug}",
                "--plain",
                "--silent",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            _fail(
                f"Missing Infisical secret '{key}' in env={env_slug}. Set it "
                f"before deploying (declared by {handler_file.name} "
                f"source(s): {' '.join(sources)}).",
            )
        print(f"  {key} ✓")


# Optional OTLP-sink env vars forwarded to deployed webhook containers. None
# of these is required — the sink is opt-in per environment, and a missing
# key just means "no OTLP exporter wired, stdout-only logging." We probe
# them so a misconfigured Infisical env surfaces at deploy time instead of
# silently dropping records once Hookdeck starts firing.
_OTEL_OPTIONAL_KEYS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    # Standard OTel headers env vars — let non-HyperDX sinks (Datadog
    # DD-API-KEY, Grafana Cloud basic auth, custom collector tokens) pass
    # arbitrary auth via the spec-compliant hook the OTLPLogExporter reads
    # automatically when no explicit ``headers=`` is supplied.
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    "HYPERDX_API_KEY",
    "HYPERDX_OTLP_ENDPOINT",
    # Telemetry collector pointer: when set, webhook containers export telemetry
    # to the collector Modal function (src/otel_collector.py) instead of a sink
    # directly. The collector holds the provider creds and fans out.
    "TELEMETRY_COLLECTOR_APP",
    "TELEMETRY_COLLECTOR_FUNCTION",
)


def _preflight_otel_log_sink_keys() -> None:
    """Report which OTLP-sink keys exist in Infisical. Never fails the deploy.

    This is an inventory of the target Infisical environment, NOT a statement
    about the app being deployed. Neither executor forwards these keys, by
    design: an unset ``TELEMETRY_COLLECTOR_APP`` selects collector mode (the
    only mode that reaches Logfire), and app containers must never carry
    provider credentials — they reach providers through the collector. A
    ``present`` key here therefore tells the operator what the collector
    deployment can read, and what a container would pick up if someone
    reintroduced env forwarding. See :func:`deploy_env`.

    Uses the same "returncode 0 + empty stdout = missing" heuristic
    documented at ``_preflight_infisical_keys`` — the ``infisical secrets
    get`` CLI exits 0 for both present and missing keys, so a
    returncode-only check would be theater.
    """
    env_slug = os.environ["INFISICAL_ENV"]
    print(
        f"Inventorying OTLP-sink keys in Infisical env={env_slug} "
        f"(not forwarded to the deployed app): {' '.join(_OTEL_OPTIONAL_KEYS)}",
    )
    any_present = False
    for key in _OTEL_OPTIONAL_KEYS:
        proc = subprocess.run(
            [
                "infisical",
                "secrets",
                "get",
                key,
                "--projectId",
                os.environ["INFISICAL_PROJECT_ID"],
                "--token",
                os.environ["INFISICAL_TOKEN"],
                f"--env={env_slug}",
                "--plain",
                "--silent",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            print(f"  {key} present in Infisical")
            any_present = True
        else:
            print(f"  {key} (not set)")
    if not any_present:
        print(f"  No OTLP-sink keys set in Infisical env={env_slug}.")


def _preflight_flox() -> None:
    """Verify the Flox environment can pin the deploy toolchain.

    Only meaningful for the default Flox transport; the Dagger wrapper already
    starts from the prebuilt toolchain image.

    Asks flox where its environment is rather than re-deriving the path from
    ``uname``: ``FLOX_ENV`` is set inside a ``--mode run`` activation and
    equals the project-local ``.flox/run/<arch>-<os>.<env>-run`` symlink, so
    reimplementing the ``arm64`` -> ``aarch64`` translation in Python would be
    a second copy of something flox already knows.
    (``scripts/conductor-workspace-setup.sh`` still derives its own; this
    avoids adding a third.)

    Three traps encoded here deliberately:

    - ``printf %s "$FLOX_ENV"`` inside ``sh -c``, never ``printenv
      FLOX_ENV``: ``printenv`` exits 1 on an unset variable, which would turn
      a diagnostic into a hard failure.
    - Every probe passes ``--``. Any ``flox`` invocation without one (e.g.
      ``flox --version``) degenerates to a silent success under the test
      suite's pass-through stub, making the probe worthless.
    - Tool resolution asks the *activated* shell. ``shutil.which`` in this
      process cannot see inside an activation.

    ``--mode run``, matching :func:`scripts.lib.flox.flox_activate_prefix` — ``--mode dev``
    resolves a different store path, so probing it would verify the wrong
    environment.
    """
    print("Preflighting Flox environment")
    try:
        flox_env = flox_preflight(REPO_ROOT, ("uv", "git"))
    except FloxEnvironmentNotActivatedError:
        print("  activation returned no FLOX_ENV; toolchain pinning unverified")
        return
    except RuntimeError as exc:
        _fail(f"Flox activation failed: {exc}")
    except (OSError, subprocess.CalledProcessError) as exc:
        _fail(f"Flox activation failed: {exc}")
    print(f"  FLOX_ENV={flox_env}")


_BUCKET_METHOD_RE = re.compile(r"WebhookModel\.([a-z_]+_get_bucket_name)")


def _preflight_gcs_buckets(
    handler_file: Path,
    sources: Iterable[str],
) -> None:
    """Verify the per-source GCS bucket exists for handlers that write to gs://.

    Worth doing up front: a missing bucket does not surface until the first
    write, i.e. on the first live Hookdeck event after a deploy that reported
    success.

    Auto-detects whether the handler routes to a per-source bucket by
    matching ``WebhookModel.<prefix>_get_bucket_name`` in its source. The
    Attio handler doesn't write to GCS, so this pattern is absent and the
    preflight is skipped.
    """
    text = handler_file.read_text()
    match = _BUCKET_METHOD_RE.search(text)
    if match is None:
        return
    bucket_method = match.group(1)

    if shutil.which("gcloud") is None:
        _fail(
            f"gcloud CLI not found — required to preflight GCS buckets for "
            f"{handler_file.stem}.",
        )

    print(f"Preflighting GCS buckets via WebhookModel.{bucket_method}()")
    for source in sources:
        module = _source_module_for(handler_file, source)
        bucket_proc = subprocess.run(
            [
                _require_uv_path(),
                "run",
                "python",
                "-c",
                (f"from {module} import Webhook\nprint(Webhook.{bucket_method}())\n"),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if bucket_proc.returncode != 0:
            _fail(
                f"Could not resolve bucket name for {source} via "
                f"{module}.Webhook.{bucket_method}().",
            )
        bucket = bucket_proc.stdout.strip()
        ls = subprocess.run(
            [
                "gcloud",
                "storage",
                "ls",
                "--project=dlthub-sandbox",
                f"gs://{bucket}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if ls.returncode != 0:
            _fail(
                f"Missing GCS bucket: gs://{bucket} (source {source}). "
                f"Create it before deploying.",
            )
        print(f"  {source}: gs://{bucket} ✓")


# ---------------------------------------------------------------------------
# Lock / backup / restore / cleanup
# ---------------------------------------------------------------------------


def _acquire_lock() -> None:
    """Atomic advisory lock via ``mkdir`` semantics.

    Serializes concurrent invocations. Two terminals can both pass the
    clean-tree preflight and then race on the handler file and the shared
    ``tmp/webhook-deploy-bak/``: one can delete the other's restore source,
    or pick up its substitution and deploy the wrong source.

    ``Path.mkdir(exist_ok=False)`` raises ``FileExistsError`` atomically on
    every POSIX filesystem. Avoids ``flock`` (not installed by default on
    macOS) and the standard race window of ``if exists ... mkdir``.
    """
    global _lock_acquired  # noqa: PLW0603 — module-level state read by atexit
    (REPO_ROOT / "tmp").mkdir(exist_ok=True)
    try:
        LOCK_DIR.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        _fail(
            f"Another scripts/webhooks-handlers-redeploy.py invocation appears to be "
            f"running (lock dir {LOCK_DIR.relative_to(REPO_ROOT)} exists). "
            f"If you are sure it is not, rmdir "
            f"{LOCK_DIR.relative_to(REPO_ROOT)} and retry.",
        )
    _lock_acquired = True


def _release_lock() -> None:
    """Best-effort lock release. Safe to call when the lock was never acquired."""
    if not _lock_acquired:
        return
    try:
        LOCK_DIR.rmdir()
    except (FileNotFoundError, OSError):
        pass


def _write_backup(handler_file: Path) -> None:
    """Snapshot the current handler so cleanup can always restore it.

    Clear any stale backups from prior runs *before* taking the fresh one.
    Otherwise a leftover backup — possibly for a different handler — would
    be the restore source, leaving the worktree dirty. Safe because the
    working-tree preflight already guaranteed ``webhooks/`` matches HEAD.
    """
    global _backup_freshly_written  # noqa: PLW0603 — read by atexit/signals
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for stale in BACKUP_DIR.glob("*.py"):
        stale.unlink()
    shutil.copyfile(handler_file, BACKUP_DIR / f"{handler_file.stem}.py")
    _backup_freshly_written = True


def _restore_handler() -> None:
    r"""Restore the current handler from its backup.

    Gated on ``_backup_freshly_written`` so an early-failure path (e.g.
    Modal preflight error) cannot copy a stale backup from a prior run on
    top of a clean worktree. ``shutil.copyfile`` always overwrites — no
    interactive-cp alias risk. Do not swap it for a helper that accepts
    ``exist_ok=False``: that resurrects the ``cp -i`` footgun the bash
    version needed ``\cp -f`` to dodge, where the restore silently refused.
    """
    if not _backup_freshly_written or _handler is None or _handler_file is None:
        return
    backup = BACKUP_DIR / f"{_handler}.py"
    if backup.exists():
        shutil.copyfile(backup, _handler_file)


def _cleanup() -> None:
    """Restore the handler and release the lock. Idempotent."""
    _restore_handler()
    _release_lock()


def _install_signal_handlers() -> None:
    """Convert SIGINT/SIGTERM into ``sys.exit`` so ``atexit`` runs.

    Without this, a SIGINT during ``modal deploy`` would terminate the
    process without firing the ``atexit``-registered cleanup, leaving the
    substituted handler file in the worktree.
    """

    def _handler(signum: int, _frame: object) -> None:
        sys.exit(128 + signum)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ---------------------------------------------------------------------------
# Deploy recipe — one description of the deploy, two executors
# ---------------------------------------------------------------------------
#
# Dagger (Mac/CI) and Flox (Conductor cloud sandboxes, where the Dagger engine
# cannot start) must differ ONLY in the isolation layer. When each executor
# carried its own hand-written argv list they drifted in ways that changed the
# deployed artifact, not just the mechanics: Flox never ran ``uv sync
# --frozen``, and its ``infisical run`` wrapper injected a superset of the
# env Dagger passes, which ``src/secrets_bootstrap.py`` bakes into the app's
# Modal Secret. A ``TELEMETRY_COLLECTOR_APP=""`` key in Infisical was enough
# to make the same commit deploy in collector mode under Dagger and in
# direct-sink mode (no Logfire) under Flox.
#
# The recipe below is the single description of *what* runs. Neither executor
# may add a step or invent an env var; both take the credential dict as a
# parameter. ``tests/scripts/test_deploy_webhook_dagger.py`` asserts parity
# step-by-step, because these already drifted once.


# The Modal token pair identifies a *workspace*, not a deploy target. Both
# executors resolve it from this one Infisical env so a prod deploy and a dev
# deploy land in the same Modal workspace — INFISICAL_ENV (which the deployed
# app reads at request time) is a payload value and deliberately does not
# select credentials here.
MODAL_TOKEN_INFISICAL_ENV = "dev"  # noqa: S105 # nosec B105 -- env slug, not a secret

# Throwaway venv for the Flox executor's `uv sync --frozen`, the counterpart of
# Dagger's `exclude=[".venv/"]` source-mount filter. Flox runs in place on the
# operator's checkout, so syncing into `.venv` would mutate it mid-deploy — and
# `uv sync` prunes: on an `--all-extras` workspace a plain sync uninstalls the
# `marketplace` extra. `tmp/` is already this script's scratch dir and is
# gitignored.
FLOX_DEPLOY_VENV = REPO_ROOT / "tmp" / "webhook-deploy-venv"


class DeployStep(NamedTuple):
    """One command in the deploy, plus whether it may see credentials.

    ``with_credentials`` is load-bearing, not decoration: Dagger attaches its
    secrets *after* ``uv sync --frozen``, so the sync runs with none. A flat
    list of argvs would let the Flox executor hand credentials to both steps
    while a parity test still passed.
    """

    argv: list[str]
    with_credentials: bool


def deploy_steps(rel: str) -> tuple[DeployStep, ...]:
    """The commands both executors run, in order, for handler ``rel``.

    ``uv run modal deploy``, never bare ``modal deploy``: bare ``modal`` runs
    outside the project venv and cannot import the ``src.*`` packages
    registered in pyproject.toml.
    """
    return (
        DeployStep(argv=["uv", "sync", "--frozen"], with_credentials=False),
        DeployStep(
            argv=["uv", "run", "--no-sync", "modal", "deploy", rel],
            with_credentials=True,
        ),
    )


def deploy_env(
    *,
    modal_token_id: str,
    modal_token_secret: str,
    infisical_token: str,
    infisical_project_id: str,
    infisical_env: str,
    infisical_host: str | None,
) -> dict[str, str]:
    """Build the credential env both executors hand to ``modal deploy``.

    Deliberately pure — no ``os.environ`` reads — so the deployed artifact is
    a function of these six arguments and nothing else. Ordering matters: the
    Dagger executor mints one content-addressed secret per entry in iteration
    order, and the parity test pins that order.

    ``INFISICAL_HOST`` is omitted (not blanked) when falsy: an empty
    ``INFISICAL_HOST`` baked into the runtime bootstrap secret confuses
    ``libs/infisical`` self-host vs. SaaS detection on the first webhook event.

    Telemetry keys are absent on purpose. ``libs.telemetry`` treats an unset
    ``TELEMETRY_COLLECTOR_APP`` as collector mode — the only mode that reaches
    Logfire — and app containers must never carry provider credentials, so
    forwarding the OTLP sink keys would silently downgrade the deployed app.
    """
    env = {
        "MODAL_TOKEN_ID": modal_token_id,
        "MODAL_TOKEN_SECRET": modal_token_secret,
        "INFISICAL_TOKEN": infisical_token,
        "INFISICAL_PROJECT_ID": infisical_project_id,
        "INFISICAL_ENV": infisical_env,
    }
    if infisical_host:
        env["INFISICAL_HOST"] = infisical_host
    return env


# Env vars the Flox executor removes from the inherited environment before
# applying ``deploy_env``. A dict merge cannot express "unset", and the
# distinction matters: ``libs/telemetry`` reads an unset
# ``TELEMETRY_COLLECTOR_APP`` as collector mode but ``""`` as opt-out, so a
# stray blank export in the operator's shell would otherwise be inherited and
# baked into the app's Modal Secret by ``src/secrets_bootstrap.py``.
#
# ``MODAL_ENVIRONMENT`` and friends: modal/config.py lets env vars beat
# ``~/.modal.toml``, so leaving them through reintroduces "same tokens,
# different deploy target". ``PYTHONPATH``/``VIRTUAL_ENV``/``UV_*``: keep the
# in-place Flox run from resolving a different interpreter or venv than the
# one ``uv sync --frozen`` just built (``UV_NO_DEV=1`` in the operator's shell
# would strip dev deps out of the deploy).
#
# NOT scrubbed: PATH, HOME, TMPDIR, SSL_CERT_FILE, NIX_*, XDG_*, FLOX_* —
# ``flox activate`` needs them.
_SECRET_PAYLOAD_KEYS: tuple[str, ...] = (
    # Everything src/secrets_bootstrap.py::_bootstrap_secret_payload() reads.
    "INFISICAL_TOKEN",
    "INFISICAL_PROJECT_ID",
    "INFISICAL_HOST",
    "INFISICAL_ENV",
    *_OTEL_OPTIONAL_KEYS,
)
_MODAL_CONTROL_KEYS: tuple[str, ...] = (
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "MODAL_ENVIRONMENT",
    "MODAL_PROFILE",
    "MODAL_CONFIG_PATH",
    "MODAL_IMAGE_BUILDER_VERSION",
    "MODAL_FORCE_BUILD",
)
_PYTHON_RESOLUTION_KEYS: tuple[str, ...] = ("PYTHONPATH", "VIRTUAL_ENV")
_SCRUB_PREFIXES: tuple[str, ...] = ("UV_",)


def deploy_env_scrub_keys() -> frozenset[str]:
    """Exact env-var names the Flox executor strips from ``os.environ``.

    Prefix-matched families (``UV_*``) are handled separately by
    :func:`_scrubbed_parent_env`; this returns only the literal names so a
    test can assert the set directly.
    """
    return frozenset(
        (*_SECRET_PAYLOAD_KEYS, *_MODAL_CONTROL_KEYS, *_PYTHON_RESOLUTION_KEYS),
    )


def _scrubbed_parent_env() -> dict[str, str]:
    """``os.environ`` minus every key the deploy must not inherit."""
    scrub = deploy_env_scrub_keys()
    return {
        key: value
        for key, value in os.environ.items()
        if key not in scrub and not key.startswith(_SCRUB_PREFIXES)
    }


def _use_dagger() -> bool:
    """Whether this host process should hand the recipe to the wrapper."""
    return env_flag(RUN_WITH_DAGGER) and not in_container_phase()


def _needs_flox_preflight() -> bool:
    """Whether this host process, rather than the image, needs Flox probing."""
    return not env_flag(RUN_WITH_DAGGER) and not in_container_phase()


# ---------------------------------------------------------------------------
# Deploy executors
# ---------------------------------------------------------------------------


def _resolve_modal_tokens() -> tuple[str, str]:
    """Pull MODAL_TOKEN_ID / MODAL_TOKEN_SECRET from Infisical for both paths.

    Each token is fetched in its own ``infisical secrets get`` call. The
    obvious alternative — ``infisical run -- printenv VAR1 VAR2`` — breaks
    on macOS where BSD ``printenv`` only prints the first matching name (a
    silent divergence from GNU ``printenv``). One subprocess per token also
    means an error message can name the specific missing var instead of
    conflating them.

    An explicit fetch, rather than letting ``infisical run`` inject the pair,
    is what makes a missing token a clean pre-deploy failure. ``infisical
    run`` exits 0 and injects nothing when a key is absent from the selected
    env; ``_preflight_env`` has already popped the operator's
    ``MODAL_TOKEN_*``, so modal/config.py would then fall back to whatever
    ``~/.modal.toml`` profile happens to be active and deploy into that
    workspace with no error at all.

    Personal Modal tokens were already popped from ``os.environ`` in
    ``_preflight_env``; the values returned here flow into Dagger
    ``set_secret`` calls or the Flox executor's explicit child env, and never
    land back in this process's own environment.
    """
    return _fetch_infisical_value("MODAL_TOKEN_ID"), _fetch_infisical_value(
        "MODAL_TOKEN_SECRET",
    )


def _fetch_infisical_value(
    name: str,
    *,
    env_slug: str = MODAL_TOKEN_INFISICAL_ENV,
) -> str:
    """Fetch one required secret value from the configured Infisical environment."""
    proc = subprocess.run(
        [
            "infisical",
            "secrets",
            "get",
            name,
            "--projectId",
            os.environ["INFISICAL_PROJECT_ID"],
            "--token",
            os.environ["INFISICAL_TOKEN"],
            f"--env={env_slug}",
            "--plain",
            "--silent",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        _fail(
            f"Could not fetch '{name}' from Infisical env={env_slug}. Is it "
            f"set in the dlthub project?",
        )
    return proc.stdout.strip()


async def _deploy_via_dagger(
    handler_file: Path,
    *,
    deploy_env: dict[str, str],
) -> None:
    """Run the single Flox recipe through the shared Dagger transport.

    The image is prebuilt from the same Flox manifest, so it already contains
    the pinned ``git`` and ``uv`` tools. The lock file pins
    the public ``gtm-linear`` git dependency that ``uv sync`` must clone (the
    repo is public, so no credentials are needed). Without git the sync
    aborts with "Git executable not found" before ``modal deploy`` runs
    (ai-8h3).

    Credentials are attached to the one container that runs the recipe. The
    commands share the same filesystem so the sync-created ``.venv`` remains
    available to the deploy command.
    """
    rel = handler_file.relative_to(REPO_ROOT).as_posix()
    steps = deploy_steps(rel)
    await run_recipe_in_container_async(
        repo_root=REPO_ROOT,
        commands=[step.argv for step in steps],
        command_secrets=[
            {} if not step.with_credentials else deploy_env for step in steps
        ],
    )


def _deploy_via_flox(handler_file: Path, *, deploy_env: dict[str, str]) -> None:
    """Run :func:`deploy_steps` in a Flox-activated shell (no Dagger engine).

    Flox is the primary execution path. It pins ``uv``/``git`` via the Nix
    store, while ``RUN_WITH_DAGGER`` selects the shared isolation wrapper.

    Scrub-then-apply, not merge: ``{**os.environ, **deploy_env}`` cannot
    express "unset", and ``libs/telemetry`` distinguishes an unset
    ``TELEMETRY_COLLECTOR_APP`` (collector mode) from ``""`` (opt out). See
    :func:`deploy_env_scrub_keys`.

    No ``infisical run`` wrapper. It is *sufficient* (there is no Dagger exec
    cache to defeat here) but not required, and it injects the whole
    environment's worth of secrets — a superset of ``deploy_env`` that
    ``src/secrets_bootstrap.py`` would bake into the deployed app, making the
    Flox-deployed artifact differ from the Dagger-deployed one.

    Calls ``uv`` directly rather than via ``_require_uv_path()``'s PATH scan:
    Flox pins an exact ``uv`` version in the activated shell, so resolving
    again would just re-discover the same activated binary. One activation
    per step mirrors Dagger's two ``with_exec``s (warm activation is
    sub-second and needs no network); collapsing them into a single
    ``sh -c 'a && b'`` would reintroduce the shell-string form this script
    bans everywhere else.
    """
    rel = handler_file.relative_to(REPO_ROOT).as_posix()
    base_env = _scrubbed_parent_env()
    # Set after the scrub (which strips every UV_*): redirect the sync into a
    # throwaway venv so an in-place deploy can never prune the operator's.
    base_env["UV_PROJECT_ENVIRONMENT"] = str(FLOX_DEPLOY_VENV)
    for step in deploy_steps(rel):
        step_env = dict(base_env)
        if step.with_credentials:
            step_env |= deploy_env
        flox_run(step.argv, repo_root=REPO_ROOT, env=step_env, clear_env=True)


def _verify_clean_restore(handler_file: Path) -> None:
    """Confirm restore left the file matching HEAD; fail loudly if not."""
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", str(handler_file)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if not status.strip():
        return
    print(
        f"ERROR: {handler_file.relative_to(REPO_ROOT)} is dirty after restore "
        f"— placeholder swap failed.",
        file=sys.stderr,
    )
    print(status, file=sys.stderr)
    subprocess.run(
        ["git", "diff", "HEAD", "--", str(handler_file)],
        cwd=REPO_ROOT,
        check=False,
    )
    sys.exit(1)


def _resolve_infisical_host() -> str | None:
    """Coerce ``INFISICAL_HOST`` to ``None`` when unset *or* empty.

    Both shapes must collapse to ``None``: ``os.environ.get(...)`` returns
    ``None`` for unset, and ``or None`` converts the falsy empty string the
    same way. Forwarding ``""`` to ``_deploy_via_dagger`` would bake an
    empty ``INFISICAL_HOST`` into the runtime bootstrap secret, which
    confuses ``libs/infisical`` self-host vs. SaaS detection at the first
    webhook event. Extracted from ``_deploy_one`` so the coercion has a
    direct unit test (tests/scripts/test_deploy_webhook_dagger.py).
    """
    return os.environ.get("INFISICAL_HOST") or None


def _deploy_one(handler_file: Path, source: str, *, deploy_env: dict[str, str]) -> None:
    """Substitute placeholder → deploy → restore from backup → verify clean.

    ``deploy_env`` is resolved once in ``main()`` and threaded through every
    source. Resolving it per source would let a token rotation or a transient
    Infisical failure part-way through ``--all`` deploy source #3 under
    different credentials than #1 and #2, splitting one handler's Modal apps across
    two workspaces.
    """
    assert _handler is not None  # set by main() before the loop
    print()
    print(f"=== Deploying {source} via {_handler} ===")

    original = handler_file.read_text()
    handler_file.write_text(original.replace(PLACEHOLDER, source))

    try:
        if _use_dagger():
            asyncio.run(_deploy_via_dagger(handler_file, deploy_env=deploy_env))
        else:
            _deploy_via_flox(handler_file, deploy_env=deploy_env)
    finally:
        # Restore unconditionally — even on deploy failure — so the next
        # iteration starts from a clean placeholder state and so a SIGINT
        # between substitute and deploy still ends with a clean worktree.
        backup = BACKUP_DIR / f"{handler_file.stem}.py"
        if backup.exists():
            shutil.copyfile(backup, handler_file)

    _verify_clean_restore(handler_file)


# ---------------------------------------------------------------------------
# Subprocess + error helpers
# ---------------------------------------------------------------------------


def _infisical_run(
    inner_cmd: list[str],
    *,
    env_slug: str,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``infisical run`` with the project's bootstrap creds.

    Bash equivalent of ``infisical run --projectId … --token … --env=<slug>
    -- <inner_cmd>``. Always called with a list (never a string and never
    via a shell), which sidesteps the "infisical-run-as-argv0" footgun where
    storing the prefix in a shell variable expands wrong under zsh.
    """
    cmd = [
        "infisical",
        "run",
        "--projectId",
        os.environ["INFISICAL_PROJECT_ID"],
        "--token",
        os.environ["INFISICAL_TOKEN"],
        f"--env={env_slug}",
        "--",
        *inner_cmd,
    ]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=capture_output,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(handlers: list[str]) -> tuple[str, str]:
    """Parse argv into ``(handler, source_or_all)``.

    The user-facing UX matches the bash predecessor:

        scripts/webhooks-handlers-redeploy.py <handler> <source>
        scripts/webhooks-handlers-redeploy.py <handler> --all

    ``--all`` is implemented as an explicit flag rather than a positional
    sentinel because argparse parses a leading-dash positional as an
    unknown option, breaking the documented invocation. The caller above
    sees a single string (either the alias name or the literal ``--all``)
    and dispatches on that, so the internal contract stays the same.
    """
    parser = argparse.ArgumentParser(
        prog="scripts/webhooks-handlers-redeploy.py",
        description=(
            "Substitute the WebhookModelToReplace placeholder, deploy via "
            "Dagger-wrapped `modal deploy`, then restore the handler. "
            "See webhooks/AGENTS.md, and this script's own docstrings, for "
            "the full set of footguns this encodes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Preconditions:\n"
            "  - INFISICAL_PROJECT_ID and INFISICAL_TOKEN exported\n"
            "    (run: set -a && source .env.local && set +a)\n"
            "  - INFISICAL_ENV exported (dev|staging|prod) — no default\n"
            "  - working tree under webhooks/ is clean\n"
            "  - required Modal secrets exist in the dlthub workspace\n"
            f"\nDiscovered handlers: {' '.join(handlers)}"
        ),
    )
    aliases = ", ".join(f"{a}={t}" for a, t in HANDLER_ALIASES.items())
    parser.add_argument(
        "handler",
        help=f"one of: {' '.join(handlers)} (aliases: {aliases})",
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="a 'Webhook as <Alias>' alias imported by the handler",
    )
    parser.add_argument(
        "--all",
        dest="all_sources",
        action="store_true",
        help="deploy every source imported by the handler",
    )
    args = parser.parse_args()
    if args.all_sources and args.source is not None:
        parser.error("specify either <source> or --all, not both")
    if args.all_sources:
        return args.handler, "--all"
    if args.source is None:
        parser.error("specify a <source> alias or pass --all")
    return args.handler, args.source


def main() -> int:
    """Run the deploy recipe without allowing container re-entry to mutate the host."""
    global _handler, _handler_file  # noqa: PLW0603 — module state for cleanup

    if in_container_phase():
        _fail("webhook deploy cannot enter through the container phase")

    handlers = _discover_handlers()
    handler, source_or_all = _parse_args(handlers)
    handler = HANDLER_ALIASES.get(handler, handler)
    if handler not in handlers:
        print(
            f"ERROR: Unknown handler: {handler}\n"
            f"  Valid handlers: {' '.join(handlers)}",
            file=sys.stderr,
        )
        return 1

    handler_file = WEBHOOKS_DIR / f"{handler}.py"
    if not handler_file.exists():
        _fail(f"Handler file not found: {handler_file}")

    valid_sources = _discover_sources(handler_file)
    if source_or_all == "--all":
        sources_to_deploy = list(valid_sources)
    elif source_or_all in valid_sources:
        sources_to_deploy = [source_or_all]
    else:
        print(
            f"ERROR: Unknown source: {source_or_all}\n"
            f"  Sources imported by {handler_file.relative_to(REPO_ROOT)}: "
            f"{' '.join(valid_sources)}",
            file=sys.stderr,
        )
        return 1

    _preflight_uv_version()
    _preflight_env()
    # Validate the primary Flox environment before taking the mutation lock.
    if _needs_flox_preflight():
        _preflight_flox()

    # Acquire lock *before* the working-tree preflight so the snapshot below
    # cannot become stale between check and mutation. Install cleanup
    # immediately after the lock so a Ctrl-C between here and the deploy
    # always releases the lock + (eventually) restores the handler.
    _acquire_lock()
    atexit.register(_cleanup)
    _install_signal_handlers()

    _preflight_working_tree()
    _preflight_modal_secrets()
    _preflight_infisical_keys(handler_file, sources_to_deploy)
    _preflight_otel_log_sink_keys()
    _preflight_gcs_buckets(handler_file, sources_to_deploy)

    # Resolve credentials before touching the working tree, and exactly once
    # for the whole run — see _deploy_one's docstring for why per-source
    # resolution is a divergence hazard rather than just two subprocesses per source.
    modal_token_id, modal_token_secret = _resolve_modal_tokens()
    resolved_deploy_env = deploy_env(
        modal_token_id=modal_token_id,
        modal_token_secret=modal_token_secret,
        infisical_token=os.environ["INFISICAL_TOKEN"],
        infisical_project_id=os.environ["INFISICAL_PROJECT_ID"],
        infisical_env=os.environ["INFISICAL_ENV"],
        infisical_host=_resolve_infisical_host(),
    )

    _handler = handler
    _handler_file = handler_file
    _write_backup(handler_file)

    for source in sources_to_deploy:
        _deploy_one(handler_file, source, deploy_env=resolved_deploy_env)

    print()
    print("All deploys complete. Working tree clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
