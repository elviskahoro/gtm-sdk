# AGENTS.md

Rules for working in this repo. `CLAUDE.md` and `WARP.md` symlink here. The repo layout, CLI surface, and adapter list are discoverable — don't expect this file to mirror them.

## Code placement

- `libs/<service>/` — wrap **one** external SDK/API. Idiomatic Python types/functions only.
- `src/` — orchestration. Multi-step flows, side effects, Modal `@app.function` / `@modal.fastapi_endpoint` decorators.
- `cli/` — Typer subapps. Parse → preflight → call `src/` → render. **No business logic.**
- `data-gen/` — independent, composable data products.
- `webhooks/` — standalone Modal apps. Deploy via `scripts/webhooks-handlers-redeploy.py` (see "Webhook deploys" — direct `modal deploy` fails on the placeholder). Do **not** register them in `src/app.py`.
- `api/specs/`, `api/samples/` — external API specs and fixture payloads. Read-only reference.
- `tmp/` — scratch only. Gitignored. Never write temp files anywhere else.

### Hard rules

- **No cross-lib imports.** `libs/<x>` must not import from `libs/<y>`. If two adapters need to coordinate, do it in `src/`. Exceptions: utilities (`libs.telemetry`, `libs.logging`, `libs.filesystem`) are importable from anywhere.
- **No orchestration in `libs/`.** Adapter modules must be callable in isolation.
- **Module boundaries are enforced by tach**, run via trunk (like every other linter — see "Linting" below). Reproduce a finding with `trunk check --filter=tach`; CI runs a full-graph `trunk check --filter=tach --all` step on every push and PR. The `dev`-group `tach>=0.35.0` pin in `pyproject.toml` stays (for `tach show`/`tach sync` ergonomics) — bump it in lockstep with the `tach@` version in `.trunk/trunk.yaml`'s `lint.enabled` and the plugin `ref` in `oss-linter-trunk-tach`'s `plugin.yaml` `known_good_version`.
- **New top-level package?** Update `[tool.setuptools.packages.find]` in `pyproject.toml` (currently `cli*`, `libs*`, `src*`).

## Public API and downstream consumers

**This repo is a library. Unreferenced here ≠ dead.** Other repos install gtm-sdk as a dependency (`ai/pyproject.toml` and `ai/projects/crm-uploader/pyproject.toml` both take it as an editable path dep), so their call sites are invisible to this repo's tests, to `ruff`, and to any dead-code scanner. Eight bot-authored "remove unused code" PRs (#362–#371) reached the wrong conclusion from exactly this blind spot and proposed deleting seven symbols `crm-uploader` calls in production.

Two mechanisms make the real surface visible. Both are cheap to check and neither is optional:

- **`libs/<x>/__init__.py`'s `__all__` is the public-API declaration.** Dead-code tooling treats an `__all__` re-export as an entrypoint — the bot's PR bodies say so verbatim ("`libs/attio/__init__.py` is empty, so the symbol was not re-exported as part of a public API"). Removing a name from an `__all__` is a breaking change, not cleanup.
- **`contracts/downstream_api.toml` records what consumers actually import**, enforced by `tests/test_downstream_contract.py` in the required `Unit tests` gate. It also pins the few private helpers consumers reach into (e.g. `libs.attio.people._search_people_raw`), which stay out of `__all__` on purpose.

**Before merging any dead-code PR**, grep both. If the symbol appears in either, close the PR. If it is genuinely consumed but absent from the contract, that is a contract bug — fix it first.

Consumer imports changed, or you added a consumer? Regenerate rather than hand-edit:

```shell
scripts/downstream-contract-sync.py ~/Documents/ai/projects/crm-uploader --write
```

That script is deliberately **not** in CI: this repo is public and `ai/` is private, so public CI can never read the consumer tree. The contract travels as committed data instead.

## Modal gotchas

- `deploy.py` stays at the repo root. Moving it under `src/` causes `src/attio/` to shadow the `attio` pip package.
- New endpoint = add the module import to `_ENDPOINT_MODULES` in `src/app.py`, otherwise its decorators don't register.
- New secret = add `"<X>_API_KEY": <x>_client.api_key_scope` to `KEY_SCOPES` in `src/secrets_bootstrap.py` (after wiring an `api_key_scope` contextvar in `libs/<x>/client.py`), then decorate the function with `@with_secrets("<X>_API_KEY")` and bind `secrets=[bootstrap_secret()]`. Do NOT use `modal.Secret.from_name(...)` — see ai-672.
- Free tier caps the app at **8 web endpoints**. Don't silently exceed it.
- App name resolves from the `MODAL_APP` env var (`src/modal_app.py`).
- **Troubleshoot a deployed app via the CLI** — `infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=<env> -- uv run modal app logs <app-name>` tails its logs; a crash-looping container (e.g. a dep missing from the image's `uv_pip_install` that's present in the local venv) prints its import traceback there.
- **A webhook image's `uv_pip_install` must list every dependency the handler transitively imports** — a package present in the local venv but absent from the minimal Modal image (e.g. `flatsplode`, the `opentelemetry-*` exporter) crash-loops the container on import and is invisible to local tests; after deploy, confirm a clean startup via `modal app logs`.

## Webhook deploys

`webhooks/export_to_attio.py`, `webhooks/export_to_gcp_etl.py`, `webhooks/export_to_gcp_raw.py`, and `webhooks/export_to_slack.py` ship one Modal app per webhook source, but each file uses a `WebhookModelToReplace` placeholder so the working tree stays source-agnostic. **`modal deploy` on the file as-is fails with `NameError: WebhookModelToReplace is not defined`.**

Use `scripts/webhooks-handlers-redeploy.py <handler> <source>` (or `<handler> --all`) to substitute the placeholder, deploy, and restore in one step. The script auto-discovers valid handlers (any `webhooks/*.py` containing the placeholder) and sources (the `Webhook as <Alias>` imports inside the handler), and preflights per-source GCS buckets when the handler routes to `gs://` (etl, raw). It encodes every footgun in the "Scripted deploy pitfalls" section below.

```shell
set -a && source .env.local && set +a   # once per shell
export INFISICAL_ENV=dev                 # explicit; no default
scripts/webhooks-handlers-redeploy.py export_to_attio CaldotcomBookingWebhook
scripts/webhooks-handlers-redeploy.py export_to_gcp_etl --all
scripts/webhooks-handlers-redeploy.py export_to_gcp_raw --all
```

The `modal deploy` step runs inside a Dagger container (matching the
`scripts/hookdeck-connection_events-dump.py` pattern) so the env that ships
images to Modal is reproducible across operators. Modal tokens flow into the
container as `dagger.set_secret(...)` values; the `infisical` CLI stays on the
host.

**`GTM_EXEC_BACKEND` selects where the deploy step runs** (default `dagger`; resolved by `resolve_exec_backend()` in `scripts/lib/env.py`). `dagger` and `flox` are two mechanisms for the *same* requirement — a pinned toolchain — so pick by environment, not by preference:

- `dagger` — the container above. Local Mac and CI.
- `flox` — `infisical run -- uv run modal deploy` wrapped in `flox activate --dir <repo> --mode run --`, so the toolchain comes from `.flox/env/manifest.lock` instead of an OCI image. **The documented path on Conductor cloud sandboxes.** It preflights (flox on PATH → activation succeeds → `.flox/run/<arch>-<os>.gtm-sdk-run/bin` exists → required tools present) and **fails loudly rather than degrading to bare `PATH`** — a sandbox can genuinely be in that state, since `scripts/conductor-workspace-setup.sh` falls back to curl installers when Flox provisioning fails.
- `host` — the same command against bare `PATH`, no pinning. For `tests/scripts/test_deploy_webhook.py` only, whose stub `infisical`/`modal`/`uv` binaries a Flox activation would shadow with the real ones.

`DAGGER_DRY_RUN=1` still works as a deprecated alias for `host` and warns. It was misnamed — it never implied a dry run, it really deployed.

**Dagger's engine cannot run on Conductor cloud sandboxes.** The cause, re-verified on kernel 6.12.76 (issue #284's investigation was on 5.10.174): `xt_comment` is absent from the kernel and cannot be loaded (no `modprobe`, no `/lib/modules`, no nftables), so CNI bridge setup fails, so `networkMode = "host"` is the only viable mode — and Dagger's per-exec telemetry proxy assumes a per-exec netns exists (`engine/engineutil/linux_namespace.go` `os.OpenFile`s the netns path with no fallback branch and no opt-out), so the first `withExec` dies. Namespace creation itself is *not* blocked — `unshare` of user/mount/pid/net/cgroup all succeed — so don't reach for "nested containers are impossible" as the explanation. Use `GTM_EXEC_BACKEND=flox` there. Local Mac Dagger deploys are unaffected. Don't re-litigate the local engine; the one thing worth rechecking is #223's Dagger Cloud entitlement, via `gh workflow run dagger-cloud-smoke.yml` (~15s, still `invalid token type` as of 2026-07-31, run 30673615946).

Each source is a separate Modal app, so deploying one source does not redeploy the others — bump them individually after shared-code changes (e.g. `libs/dlt/`) or stale containers will keep importing removed symbols. Do not commit the substituted form; an `atexit`/signal-driven cleanup restores the placeholder even if `modal deploy` fails or the script is interrupted (Ctrl-C, SIGTERM).

The contract every concrete `src/<source>/webhook/*.py` `Webhook` class must satisfy lives at `libs/webhook/protocol.py` as `WebhookModelProtocol` (a `typing.Protocol`), and `tests/libs/webhook/test_protocol_conformance.py` enforces it across all five sources. Each handler's `TYPE_CHECKING` block aliases `WebhookModelTypeCheckShim` (a concrete `BaseModel` stand-in defined alongside the Protocol) as `WebhookModelToReplace` so pyright sees the full surface — Pydantic methods (`model_rebuild`/`model_validate`) and the contract methods — in the unsubstituted source tree. New sources: extend `protocol.py` only if you add a contract method; otherwise just implement the existing surface on the new `Webhook` class and add a parametrize entry to the conformance test.

**Validate webhook models against a real captured payload, not hand-authored fixtures.** A synthetic cal.com fixture (`start`/`end`/`hosts`) diverged from the real v2 shape (`startTime`/`endTime`/`organizer`, attendees without `displayEmail`/`absent`), so every test passed while live `BOOKING_CREATED` events 422'd in production (silently, for Attio too) — capture a redacted real payload as a fixture and cross-check field names + the full trigger list against cal.com's webhook reference (<https://cal.com/docs/developing/guides/automation/webhooks>); note it defines many triggers we don't yet handle (e.g. `BOOKING_REJECTED`, `BOOKING_PAID`, `FORM_SUBMITTED`).

### Scripted deploy pitfalls

The pitfalls below explain why `scripts/webhooks-handlers-redeploy.py` is shaped the way it is. The first version was bash; the Python rewrite preserves every mitigation as an explicit module-level idiom. Keep them here as design rationale for anyone touching the script:

- **Shebang is plain `python3`, not `uv run python`, plus a self-resolving bootstrap.** `[tool.uv] required-version` makes *any* incompatible `uv` refuse before Python starts, so `uv run python` can't survive a pyenv shim shadowing a compatible install ahead of it on PATH. `_bootstrap_uv()` resolves a compatible `uv` via `scripts/lib/uv_resolve.py` (scans all of PATH, not just the first match) and `os.execv`s into it; see the function's own docstring for why it's gated on both `__name__ == "__main__"` and a `sys.prefix`-vs-`.venv` check. Does **not** protect an explicitly-typed `uv run scripts/webhooks-handlers-redeploy.py ...` against an already-incompatible shell `uv` — only direct execution and freshly-provisioned workspaces are covered.
- **`os.environ.pop("MODAL_TOKEN_ID"/"MODAL_TOKEN_SECRET")` before any `infisical run`.** Otherwise the parent shell's personal Modal tokens win over the dlthub-workspace tokens Infisical injects, and deploys land in the wrong workspace. (Bash equivalent: `unset MODAL_TOKEN_ID MODAL_TOKEN_SECRET`.)
- **Wrap with `uv run modal deploy`, not bare `modal deploy`.** Bare `modal` runs outside the project venv and can't import `src.*` packages registered in `pyproject.toml` → `ModuleNotFoundError: No module named 'src.fathom'`. Applies inside the Dagger container and on the `flox`/`host` backends alike — except that the `flox` backend passes bare `uv` (so Flox's `PATH` resolves its pinned copy) while `host` goes through `_require_uv_path()`, which is the only thing guaranteeing a version-compatible `uv` there.
- **Use `shutil.copyfile` (always overwrites) for restore.** The bash version needed `\cp -f` to dodge `cp -i` aliases that would silently refuse the restore; Python's `shutil.copyfile` has no equivalent shadowing risk. A refactor that swaps it for a helper accepting `exist_ok=False` would resurrect the original footgun.
- **Always invoke `infisical run` with a list-arg subprocess, never a string.** The Python subprocess API only accepts argv lists when `shell=False`, which sidesteps the bash gotcha where storing `infisical run --token … --` in a variable made zsh treat the whole thing as `argv[0]` and leaked the service token to stderr/shell history. Never set `shell=True`.
- **Preflight Modal secrets, Infisical keys, and GCS buckets before the deploy loop.** A missing `modal.Secret.from_name(...)` aborts after the image build; a missing Infisical key fails on the first Hookdeck event after deploy; a missing GCS bucket aborts at first write. The script calls `modal secret list --json` (via `infisical run`), `infisical secrets get` per key, and `gcloud storage ls --project=dlthub-sandbox` per bucket before touching the handler file.
- **`atexit`-registered cleanup, gated on `_BACKUP_FRESHLY_WRITTEN`, scoped to the current handler.** Restore the file even if the deploy raised, was Ctrl-C'd, or was SIGTERM'd. The gate prevents an early-failure exit from copying a stale backup from a prior run on top of a clean worktree. Signal handlers route SIGINT/SIGTERM through `sys.exit` so `atexit` fires (the default signal disposition would skip it).
- **Serialize concurrent invocations of the deploy helper.** Two terminals can both pass the clean-tree preflight and then race on the same handler file and shared `tmp/webhook-deploy-bak/` state — one process can delete the other's restore source, or one deploy can pick up the other's substitution. `scripts/webhooks-handlers-redeploy.py` uses an atomic `LOCK_DIR.mkdir(exist_ok=False)` as a portable advisory lock and releases it from the `atexit` cleanup.
- **Install `git` in the Dagger container before `uv sync --frozen`.** The `uv` base image ships no git, but `pyproject.toml` pins `gtm-linear` to a public git rev, so `uv sync` shells out to git and dies with "Git executable not found" before `modal deploy` runs. Install it via a single combined `apt-get update && apt-get install -y --no-install-recommends git` exec placed *before* the source mount so the layer caches on the base image alone; keep `update` + `install` in one exec or a stale apt index gets reused against a fresh install. The repo being public means no git credentials are needed inside the container. (ai-8h3)
- **`DAGGER_BASE_IMAGE` pins an exact `uv` release, not just a Python tag.** The unversioned `bookworm-slim` tag drifted to `uv` 0.9.30, below the required `>=0.11.8,<0.12` floor, rejecting `uv sync --frozen` in-container before `modal deploy` ran — the host-side `_bootstrap_uv()` doesn't help there. Keep the pin (`0.11.29`) in lockstep with `[tool.uv] required-version`.
- **`dagger.dag.set_secret(name, value)` caches `with_exec` on `name`, not the plaintext.** Reusing a name while its value changes (rotated token, different Infisical project) leaves the cache key unchanged, so Dagger silently replays a *prior* `modal deploy` while reporting success. `_content_addressed_secret_name` hashes the value into the name so it always busts.

### Registry

`gtm webhook sync` regenerates `webhooks/registry.yaml` (gitignored) by joining `modal app list` with the Hookdeck API. Run it after any deploy or Hookdeck wiring change. Use `gtm webhook list` to inspect the cached registry. The file is gitignored because it contains personal Modal URLs and Hookdeck IDs that don't belong in OSS — see `webhooks/README.md`.

## Workspace setup (Conductor)

`.conductor/settings.toml`'s `setup` is a thin shim: it sets up `~/.conductor-setup.log` and runs `scripts/conductor-workspace-setup.sh`, where all provisioning lives. On Linux cloud sandboxes, `dolt`/`uv`/`infisical`/`gh` and flake-pinned `bd`/`roborev` come from the committed Flox environment (`.flox/env/manifest.toml` + `manifest.lock` pin versions; `flox activate --mode run` puts them on PATH) — edit the manifest via `flox install`/`flox edit`, never by hand-syncing versions. macOS workspaces without Flox fall back to the original curl installers for `dolt`/`infisical`/`bd`/`roborev`. `uv` is the one exception: presence on PATH isn't enough (an incompatible pyenv shim can shadow a compatible install), so the fallback delegates to `scripts/lib/uv_resolve.py` to check actual version compatibility and, if needed, installs a pinned release (`UV_PINNED_VERSION`, kept in lockstep with `.flox/env/manifest.toml`'s `uv.version`) rather than unpinned latest — see "Scripted deploy pitfalls" above. The sandboxes have no running systemd and no `/dev/fd`; the script creates the `/dev/fd` symlink and starts `nix-daemon` by hand — don't "simplify" those steps away.

## Telemetry

OTEL via `libs/telemetry.py`, two modes. **Collector fan-out is the default** — the collector app name is hard-coded (`DEFAULT_COLLECTOR_APP = "otel-collector"` in `libs/telemetry.py`), so no env wiring is needed; override the app with `TELEMETRY_COLLECTOR_APP=<name>` (optional `TELEMETRY_COLLECTOR_FUNCTION`). A custom OTEL exporter serializes each batch and fire-and-forget `.spawn()`s the collector Modal function (`src/otel_collector.py`) over Modal RPC (no public endpoint). That function feeds the bytes to a real OpenTelemetry Collector running as a **localhost sidecar** in the same always-warm (`min_containers=1`) container, which fans out to **all** configured providers (Dash0 + HyperDX + Logfire + Grafana) with batching/retry/queue. Provider creds live on the collector only; deploy it standalone with `modal deploy src/otel_collector.py`. **Direct single-sink fallback** — opt out with `TELEMETRY_COLLECTOR_APP=""`: one OTLP sink via `HYPERDX_API_KEY` / `HYPERDX_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_ENDPOINT`. **This fallback has no Logfire exporter** — Logfire is reachable only through the collector, so an app in direct mode silently sends nothing to Logfire (the bug that made "no logs in Logfire": producers were never in collector mode). Neither configured → no-op; don't add fallback logging "just in case."

## Secrets (Infisical)

`.env.local` at the repo root holds `INFISICAL_TOKEN` and `INFISICAL_PROJECT_ID`. There is no `.infisical.json`, so the CLI does not auto-detect the project — pass flags explicitly or source the env file first:

```shell
set -a && source .env.local && set +a
infisical secrets --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=dev
infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=dev -- <cmd>
```

Conductor workspaces get `.env.local` copied in at provisioning; the parent `ai/` repo's `.env*` files are not copied. Never fall back to 1Password unless the user explicitly asks.

### Roborev Codex authentication

`git roborev review --wait` (see "Session Completion") is configured in
`.roborev.toml` to invoke the standalone `codex` CLI. Make sure the Codex CLI
is installed and authenticated in the environment where Roborev runs. Roborev
does not require `ANTHROPIC_API_KEY`; Claude Code credentials are unrelated to
this review path.

### `gh` CLI auth for commenting on / closing GitHub issues (not an Infisical secret)

`gh` (GitHub CLI, provisioned via the Flox environment) reads `GH_TOKEN` (or `GITHUB_TOKEN`) straight from the environment — no `gh auth login` needed, and no interactive browser flow works in a headless sandbox anyway. This is a personal PAT (classic PAT with `repo` scope, or a fine-grained PAT scoped to this repo with **Issues: Read and write**), not a shared team secret, so it does not go through Infisical/`secrets_bootstrap.py`:

- **Conductor**: set `GH_TOKEN` under `[environment_variables]` in user-level Conductor settings or another ignored local configuration file. The tracked `.conductor/settings.local.toml` is a reviewed repository baseline and must not contain credentials or machine-specific secrets. Conductor injects `[environment_variables]` directly into the agent's shell, so `gh` picks it up on every invocation without a manual `source` step.

## Script Entrypoints

- Repo-local scripts that are meant to run under `infisical run -- <cmd>` should be directly executable and use a uv shebang when practical.
- Put the canonical Infisical example string in `scripts/lib/env.py` and reuse it from script docstrings and error messages instead of hand-writing variants.
- If a script cannot be made directly executable, say why in the usage text. Do not silently fall back to `uv run python scripts/...` unless there is a concrete technical reason.
- Usage examples for scripts that rely on Infisical must show `--projectId`, `--token`, and `--env`, or explicitly say the script depends on `infisical init`.

## Package management

**Use `uv`. Never `pip`, `pip3`, or `python3 -m pip`.** Bare pip bypasses `uv.lock` and causes environment drift.

- `uv sync` — install from lock.
- `uv pip install <pkg>` — add a dep (updates lock).
- `uv run <cmd>` — run inside the env.

## Path anchoring

When a script reads/writes files that live beside it, anchor on `Path(__file__).resolve().parent`, not the CWD. `uv run path/to/script.py` does **not** chdir — relative paths resolve from wherever the user invoked the command, not the script's folder. This silently writes files to the wrong place.

```python
SCRIPT_DIR = Path(__file__).resolve().parent
(SCRIPT_DIR / "output.txt").write_text(...)
```

## Documentation

**Do not create summary, investigation, or "what I did" `.md` files.** Live documentation goes in code:

- Docstrings explain *why*, not *what*.
- Comments document decisions and gotchas inline.
- Notable releases → `docs/changelog/` on the docs site.
- Architectural decisions → design artifacts in the parent `ai/` repo's `design/`, not loose `.md` here.
- **Never hand-mirror enumerable inventories** (adapter tables, CLI trees, endpoint lists) in `README.md` or this file — they rot. Point at `uv run gtm --help` / `ls libs/` and the docs site instead.

If you finish a task and want to summarize, output it as your final response. Don't write a file.

### Docs site (`docs/`)

`docs/` is the published documentation site — the no-summary-`.md` rule does not apply there. Local preview: `npm i -g mint`, then `mint dev` inside `docs/` (Node 24 via `docs/.node-version`; mint breaks on Node 25+).

- Every page is `.mdx` with `title` + one-line `description` frontmatter (the description becomes the page's llms.txt entry) and no body H1. `scripts/docs-pages-lint.py` enforces this.
- **`docs/cli/` is generated — never hand-edit** (except `cli/index.mdx`). Change the `help=` strings in `cli/` and run `uv run scripts/docs-cli_reference-generate.py`. CI (`docs-checks.yml`) fails on drift.
- Changed a `libs/` adapter's public surface, a Modal deployment flow, or webhook wiring? Update the matching `docs/` page in the same PR.
- Moving or renaming a page? Add a `redirects` entry in `docs/docs.json` in the same PR — published URLs never die.
- Never put personal infra in `docs/`: no real Modal URLs, Hookdeck IDs, Infisical project IDs, GCS bucket names, or local paths. Placeholders are `<UPPER_SNAKE>`.

## Git

- **Branches**: `agent/<slug>`. Never `claude/*` or other provider-specific prefixes.
  - **Exception — Linear-initiated branches:** When an agent is kicked off from a Linear ticket, the branch is typically pre-created with a `feature/` prefix (e.g., `feature/eng-1234-add-email-validation`). Keep the existing branch name as-is — do not rename or override it to `agent/...`. Linear's ticket ↔ branch linkage depends on the original name. When picking up a handoff, check the current branch first (`git branch --show-current`); if it already starts with `feature/`, continue committing to it rather than creating a new `agent/` branch.
- **Worktrees**: `worktrees/<branch-name>` at the repo root. Ensure `worktrees/` exists and is gitignored. Never use `.git/modules/*` paths as user-facing worktree locations.
- **Commits/PRs**: never add AI co-author trailers (`Co-Authored-By: Claude/Oz/...`). Human authors only.

## Linting

All linters/formatters run via **trunk**, not as bare binaries. `yamllint`, `ruff`, `checkov`, `shellcheck`, `bandit`, `actionlint`, `prettier`, `mypy`, etc. live in trunk's sandbox — invoking them directly will `command not found` or use the wrong config. Reproduce a finding with `trunk check --filter=<tool> <path>`; format with `trunk fmt <path>`.

## Testing

`uv run pytest`. Importlib mode is already configured. Mirror the source layout when adding tests.

### Adding a pytest plugin

The unit CI container runs with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, so a plugin that is merely installed is **silently inert** — the suite still passes, minus whatever the plugin provided. It has to be named explicitly. Two traps, both learned by burning a CI run:

- **Put the `-p` flag in `addopts` (`pyproject.toml`), not in `PYTEST_CMD`.** On a pull request, `tests-unit.yml` deliberately executes the **base branch's** copy of `.github/workflows/ci/pytest_dagger.py` — PR-authored CI code must never run with trusted Namespace/registry credentials. A flag added there is therefore ignored until it lands on `main`, and the required `Unit tests` gate blocks that merge. `addopts` is read from the source tree, so it applies to PR, `main` and local runs alike.
- **Use the plugin's entry-point name, not its module name.** Where autoload is on (local runs, the integration job) the plugin is already registered under its entry-point name; naming the module registers the same module twice and pytest aborts with `ValueError: Plugin already registered under a different name`. For Hypothesis that means `-p hypothesispytest`, not `-p _hypothesis_pytestplugin`.

Back the result with a test rather than a comment — `tests/test_hypothesis_plugin.py` fails loudly if the plugin stops loading, in any environment.

### Property-based tests (Hypothesis)

Files are named `test_<module>_properties.py` and sit beside the example tests they complement — they do not replace them. Profiles live in `tests/conftest.py`, selected with `HYPOTHESIS_PROFILE` (our env var, not a Hypothesis feature — Hypothesis autodetects `CI`, which `dlt`/`modal`/`logfire`/`rich` also read, so setting that would change unrelated behavior):

```shell
uv run pytest --hypothesis-show-statistics    # dev: 50 examples, 400ms deadline
HYPOTHESIS_PROFILE=nightly uv run pytest      # 1000 examples, randomized
```

CI uses the `ci` profile: `derandomize=True` and `database=None`, both inherited from Hypothesis's built-in `ci` profile. Derandomizing is not optional here — CI has no `pytest-timeout`, no `--maxfail` and no job `timeout-minutes`, and Trunk.io reports any intermittent failure as a flake under a stable test ID. Seeding from a hash of the test function means a green run stays green and a failure reproduces exactly.

Reach for a property when the claim is *universal* ("never raises", "idempotent", "round-trips", "output is always lowercase"). Two rules worth knowing before you write one:

- **Finite, enumerable domain → check it exhaustively with a loop, not `st.sampled_from`.** `max_examples` caps the draws below the domain size, and under `derandomize=True` the same subset is drawn forever, leaving the rest permanently untested. Hypothesis earns its place on *unbounded* domains.
- **Watch for vacuous properties.** If the interesting branch only runs on a successful parse, bare `st.text()` will miss it nearly every time. Generate realistic inputs, and use `hypothesis.event()` so the branch split appears in `--hypothesis-show-statistics` instead of being assumed.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- **When handed a bead prompt, claim it FIRST.** If the task references a bead ID (e.g. a prompt like `○ ai-5ph ● P2 hermes: deploy hermes-agent to railway with slack socket mode`), immediately run `bd update <id> --claim` and mark it in progress (`bd update <id> --status in_progress`) before doing any other work. Do not start the task while the bead is still unclaimed/open.
- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, walk the checklist below. Pushing is a shared-state action — the rules differ by branch.

**WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds. Added/renamed a CLI command, adapter, endpoint, or webhook handler? Run `uv run scripts/docs-cli_reference-generate.py` and grep README/AGENTS/docs for the old name.
3. **Update issue status** - Close finished work, update in-progress items
4. **Commit** locally with a clear message
5. **Push policy (branch-aware):**
   - **Roborev gate (applies to ALL branches):** Never `git push` to origin without running `git roborev review --wait` against HEAD first and confirming a clean review. If roborev is unavailable or fails to run, say so and ask before pushing.
   - On `agent/*`, `feature/*`, or `conductor/*` branches: after the roborev gate passes, you MAY `git pull --rebase && git push` without asking — these are scratch branches owned by the current task.
   - On `main`, `master`, or any release/protected branch: **DO NOT push without explicit user confirmation.** Stop after the commit, say what would be pushed, and ask. The user pushing themselves is the default.
   - If unsure which category the branch falls into, treat it as protected and ask.
6. **Clean up** - Clear stashes, prune remote branches (only after push is authorized)
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Committing locally is reversible; pushing to a shared branch is not. Match the action to the blast radius.
- NEVER force-push without an explicit ask, regardless of branch.
- If a push fails, surface the error and ask before resolving — do not retry in a loop or rewrite history.
<!-- END BEADS INTEGRATION -->
