# gtm

Go-To-Market SDK + CLI for account research, enrichment, CRM sync, and outreach. Layered architecture: thin CLI → workflow orchestration → single-SDK adapters. Deployable as Modal serverless functions; consumable as an editable Python package or git submodule.

- Package name: `gtm` (entrypoint script: `gtm = cli.main:run`)
- Python: `>=3.13,<3.14`
- Package manager: **`uv` only** (never bare `pip`)
- License: MIT
- Docs: <https://elviskahoro.mintlify.app> (source in [`docs/`](docs/))

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

### Layer rules (enforced)

- `libs/<x>/` wraps **one** external SDK or API. Idiomatic Python types/functions. **No `libs/<x>` may import from `libs/<y>`.**
- `src/` chains adapters into workflows. Modal `@app.function` / `@modal.fastapi_endpoint` decorators live here.
- `cli/` is Typer-only: parse args → preflight → call into `src/` → render. No business logic.
- `data-gen/` products are independent; do not depend on each other.

Anti-patterns: orchestration inside `libs/`; business logic inside `cli/`; cross-lib imports.

## Adapters (`libs/`)

One directory per external service (Attio, Apollo, Exa, Parallel, Fathom, Granola, …) plus a few
internal utility libs. The list is discoverable — this README does not mirror it:

```bash
ls libs/
```

Every adapter follows the same pattern: `get_client()` with three-tier API-key resolution —
explicit `api_key=` argument → `api_key_scope` contextvar → env var.

## Orchestration (`src/`)

- `src/app.py` — Modal `App` definition, image build, endpoint-module registration
  (`_ENDPOINT_MODULES`). **Edit here when adding new Modal endpoints.**
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

Contract: structured success data is JSON on stdout, errors/logs on stderr; mutating
commands preview by default when they expose an execution flag.

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

## Modal deployment

```bash
uv run modal deploy deploy.py
```

- `deploy.py` lives at the repo root **on purpose** — moving it under `src/` causes `src/attio/` to shadow the pip `attio` package.
- App name resolves from `MODAL_APP` env var; falls back to `gtm-sdk`.
- Image is debian_slim + Python 3.13 with a pinned subset of deps and local `libs/` + `src/` mounted via `add_local_python_source`.
- Secrets: hydrated at call time from Infisical via `src/secrets_bootstrap.py` (`@with_secrets` + `bootstrap_secret()`) — no named Modal `Secret.from_name` bindings.
- Free tier cap: **8 web endpoints** total. Parallel endpoints are gated behind a plan upgrade.
- Endpoint modules are imported in `src/app.py` for decorator registration — when adding a new endpoint module, add the import there.

Build env vars baked into the image: `AI_BUILD_GIT_SHA`, `AI_DEPLOYED_AT`.

## Webhooks

Standalone Modal apps under `webhooks/` — one app per (handler, source) pair via the
`WebhookModelToReplace` placeholder: `export_to_attio.py`, `export_to_gcp_etl.py`,
`export_to_gcp_raw.py`, `export_to_slack.py`.

Deploy with `scripts/webhooks-handlers-redeploy.py <handler> <source>` (or `--all`) — never
`modal deploy webhooks/<file>.py` directly (it fails on the placeholder). Full runbook:
[`webhooks/README.md`](webhooks/README.md).

## Telemetry

OTEL traces and logs via `libs/telemetry.py`. The default mode fans out through a collector Modal
app (`src/otel_collector.py`, deployed standalone) to all configured providers — Dash0, HyperDX,
Logfire, Grafana; a direct single-sink fallback (`TELEMETRY_COLLECTOR_APP=""`) exists for local
dev. Neither configured → no-op; telemetry is never load-bearing.

Setup guides: [`docs/telemetry/`](docs/telemetry/) (Dash0, Grafana Cloud).

## Conventions

- **Temp files**: `tmp/` only. Never alongside source.
- **Branches**: `agent/<slug>` for agent-created branches. Never `claude/*`.
- **Worktrees**: under `worktrees/<branch-name>`. Never use `.git/modules/*` paths as user-facing worktrees.
- **Commits**: never add AI co-author trailers (`Co-Authored-By: Claude/Oz/...`). Human authors only.
- **Documentation**: live in code (docstrings, README per major module); the published docs site lives in [`docs/`](docs/). Do **not** create summary/investigation `.md` files.
- **Path anchoring in scripts**: anchor file I/O on `Path(__file__).resolve().parent`, never the CWD — `uv run path/to/script.py` does not chdir.

## Testing

- `pytest` with `--import-mode=importlib` (already in `pyproject.toml`).
- Layout mirrors source: `tests/cli/`, `tests/libs/`, `tests/src/`, `tests/integration/`.
- Integration smoke: `tests/integration/test_gtm_remote_smoke.py`.
- `S101` (assert) is allowed in `tests/**` (ruff per-file ignore).

## Agent guidance

When adding functionality:

1. **External SDK call?** → New file in `libs/<service>/`. Wrap one SDK only. No cross-lib imports.
2. **Multi-step flow / Modal endpoint?** → `src/<service>/`. Register module import in `src/app.py` if it defines endpoints.
3. **User-facing command?** → `cli/<group>/`. Typer subapp. Call into `src/`. Wire into `cli/main.py` via `app.add_typer(...)`.
4. **Standalone data product?** → `data-gen/<product>/`. Self-contained.
5. **Webhook handler?** → `webhooks/<name>.py`. Independent Modal app.

See `AGENTS.md` (symlinked from `CLAUDE.md`) for the authoritative version of these rules.
