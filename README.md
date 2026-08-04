# gtm

Go-To-Market SDK + CLI for account research, enrichment, CRM sync, and outreach. Layered architecture: thin CLI → workflow orchestration → single-SDK adapters. Deployable as Modal serverless functions; consumable as an editable Python package or git submodule.

- Package name: `gtm` (entrypoint script: `gtm = cli.main:run`)
- Python: `>=3.13,<3.14`
- Package manager: **`uv` only** (never bare `pip`)
- License: MIT
- Docs: [`docs/`](docs/)

## Layout

```txt
gtm-sdk/
├── cli/         # Thin command surface (Typer). Parses flags, preflight, calls src/.
├── src/         # Workflow orchestration. Chains libs/ adapters. Modal endpoints register here.
├── libs/        # Single-SDK adapters. One folder per external service. NO cross-lib imports.
├── data-gen/    # Reusable data generation/enrichment pipelines (independent, composable).
├── webhooks/    # Standalone Modal webhook handlers (Attio, GCS raw/ETL, Slack).
├── api/
│   ├── specs/   # External API OpenAPI specs (e.g. caldotcom, sanity)
│   └── samples/ # Sample payloads (rb2b, caldotcom, fathom, octolens)
├── tests/       # pytest, importlib mode. Mirrors src/, libs/, cli/.
├── tmp/         # Gitignored scratch. ALL temporary files go here.
├── worktrees/   # Gitignored. All git worktrees under this dir.
├── deploy.py    # Modal deploy entrypoint (must stay at root — avoids `attio` pkg shadowing).
├── pyproject.toml
└── uv.lock
```

### Layer rules

The authoritative placement and boundary rules for contributors are in
[`AGENTS.md`](AGENTS.md), with directory-specific guidance in
[`webhooks/AGENTS.md`](webhooks/AGENTS.md), [`docs/AGENTS.md`](docs/AGENTS.md),
and [`tests/AGENTS.md`](tests/AGENTS.md).

## Adapters (`libs/`)

One directory per external service (Attio, Apollo, Exa, Parallel, Fathom, Granola, …) plus a few
internal utility libs. The list is discoverable — this README does not mirror it:

```bash
ls libs/
```

Every adapter follows the same pattern: `get_client()` with three-tier API-key resolution —
explicit `api_key=` argument → `api_key_scope` contextvar → env var.

## Orchestration (`src/`)

- `src/app.py` — endpoint-module registration (`_ENDPOINT_MODULES`); re-exports
  `app`, `image` from `src.modal_runtime`. **Edit here when adding new Modal endpoints.**
- `src/modal_runtime.py` — Modal `App` definition and image build.
- `src/modal_app.py` — `MODAL_APP` name (env-overridable via `MODAL_APP`, default `gtm-sdk`).
- `src/secrets_bootstrap.py` — Infisical-backed secret hydration for Modal functions
  (`KEY_SCOPES`, `@with_secrets`, `bootstrap_secret()`).
- One package per domain (`src/accounts/`, `src/attio/`, `src/apollo/`, …) — discoverable via `ls src/`.

## CLI surface

Run via `uv run gtm <group> <command>` (or `uv run python -m cli.main`). The command tree is
discoverable — this README does not mirror it:

```bash
uv run gtm --help                # list command groups
uv run gtm <group> --help        # list commands in a group
```

Contract: success data is JSON on stdout, errors/logs on stderr; mutating commands preview by
default and require an explicit flag to execute.

CLI helpers: `cli/json_encoder.py`, `cli/json_validation.py`. CLI emits OTEL events (`cli.usage_error` on exit code 2).

## Install

### As an editable submodule (preferred when consumed from another repo)

```bash
git submodule add git@github.com:elviskahoro/gtm-sdk.git gtm-sdk
```

In the parent `pyproject.toml`:

```toml
[tool.uv.sources]
gtm = { path = "gtm-sdk", editable = true }
```

Then `uv sync`. All `cli`, `src`, `libs` packages become importable.

### Standalone

```bash
git clone git@github.com:elviskahoro/gtm-sdk.git
cd gtm-sdk
uv sync
```

### Enable Entire session capture (per clone)

Git hooks aren't committed, so after cloning on a new device wire up Entire
(agent-session checkpoints) plus the anti-AI-co-author enforcement in one step:

```bash
scripts/entire-hooks-setup.py
```

Install the Entire CLI (`curl -fsSL https://entire.io/install.sh | bash`) and run
`entire login` first. The script is idempotent — safe to re-run.

## Common commands

```bash
uv sync                          # install/lock deps
uv run gtm --help                # CLI help
uv run gtm <group> <cmd> --help  # subcommand help
uv run pytest                    # full test suite (importlib mode)
uv run pytest tests/cli          # subset
trunk check --all                # lint + typecheck (ruff, etc.)
```

For local Bazel iteration, run one target or package rather than the whole
graph, for example `bazel test //tests/libs/attio:TARGET --config=ci` or
`bazel test //tests/libs/attio/... --config=ci`. The required CI status uses a
conservative diff classifier: documentation and approved metadata-only changes
report a successful skip, while source, tests, dependency/toolchain, generated,
Bazel, script, and workflow changes run `bazel test //...`. A manual workflow
dispatch always forces the full suite.

## Modal deployment

```bash
uv run modal deploy deploy.py
```

Deployment constraints and Modal-specific gotchas are maintained in
[`AGENTS.md`](AGENTS.md).

Build env vars baked into the image: `AI_BUILD_GIT_SHA`, `AI_DEPLOYED_AT`.

## Webhooks

Standalone Modal apps under `webhooks/` — one app per (handler, source) pair via the
`WebhookModelToReplace` placeholder: `export_to_attio.py`, `export_to_gcp_etl.py`,
`export_to_gcp_raw.py`, `export_to_slack.py`.

Deploy with `scripts/webhooks-handlers-redeploy.py <handler> <source>` (or `--all`) — never
`modal deploy webhooks/<file>.py` directly (it fails on the placeholder). Full runbook:
[`webhooks/README.md`](webhooks/README.md); rules for agents working there:
[`webhooks/AGENTS.md`](webhooks/AGENTS.md).

## Telemetry

OTEL traces and logs are provided by `libs/telemetry.py`; see the
[`telemetry documentation`](docs/telemetry/) for configuration and the
authoritative contributor guidance in [`AGENTS.md`](AGENTS.md).

Setup guides: [`docs/telemetry/`](docs/telemetry/) (Dash0, Grafana Cloud).

## Conventions, testing, and agent guidance

Repository conventions, testing requirements, and contributor workflow are
maintained in [`AGENTS.md`](AGENTS.md) and the scoped guidance files linked
there.
