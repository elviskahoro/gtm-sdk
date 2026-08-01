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

Each `webhooks/export_to_*.py` handler ships one Modal app per webhook source, but uses a `WebhookModelToReplace` placeholder so the working tree stays source-agnostic. **`modal deploy` on the file as-is fails with `NameError: WebhookModelToReplace is not defined`.**

Use `scripts/webhooks-handlers-redeploy.py <handler> <source>` (or `--all`) to substitute, deploy, and restore in one step. It auto-discovers handlers (any `webhooks/*.py` with the placeholder) and sources (the `Webhook as <Alias>` imports inside), preflights per-source GCS buckets when the handler routes to `gs://`, and encodes every footgun in "Scripted deploy pitfalls" below.

```shell
set -a && source .env.local && set +a   # once per shell
export INFISICAL_ENV=dev                 # explicit; no default
scripts/webhooks-handlers-redeploy.py export_to_attio CaldotcomBookingWebhook
scripts/webhooks-handlers-redeploy.py export_to_gcp_etl --all
scripts/webhooks-handlers-redeploy.py export_to_gcp_raw --all
```

**One recipe, two executors.** The deploy is data: `deploy_steps()` (`uv sync --frozen`, then `uv run modal deploy <rel>`) and `deploy_env()` (the Modal + Infisical credential vars, and nothing else). Executors only pick the isolation layer — Dagger runs the recipe in a container; `GTM_DEPLOY_VIA_FLOX=1` runs it under `flox activate --dir . --mode run --` on the host, one activation per step. A parity test pins both to the same argvs and the same credential surface, because these two hand-written argv lists drifted into deploying *different apps* once already. Add a step or a var to the recipe, never to an executor. Select via `env_flag()`, so `GTM_DEPLOY_VIA_FLOX=true` raises instead of silently picking Dagger.

**The Flox executor scrubs the environment, it does not merge.** A dict merge cannot express "unset", and `libs/telemetry` reads an unset `TELEMETRY_COLLECTOR_APP` as collector mode but `""` as opt-out — so a stray blank export in the operator's shell would be baked into the app's Modal Secret and silently cost that app Logfire. `deploy_env_scrub_keys()` carries the list and the per-family reasoning.

Two asymmetries stay real. **Filesystem isolation:** Dagger copies the tree excluding `.venv/`; Flox runs in place, so the executor points `UV_PROJECT_ENVIRONMENT` at a throwaway `tmp/webhook-deploy-venv` — without it `uv sync --frozen` would *prune* the operator's live `.venv`. **Preflights** run on the bare host PATH, outside any activation.

**Dagger does not work on Conductor cloud sandboxes** (#284; cause corrected in #443 — do not reinvestigate). The chain: `xt_comment` is absent from the kernel and unloadable, so CNI bridge setup fails, so `networkMode = "host"` is forced, and Dagger's per-exec telemetry proxy assumes a per-exec netns and errors with no fallback. Namespace creation demonstrably works — the "nested runc fails at the kernel level" cause recorded here previously is wrong and dead-ends the next reader. Flox (`.flox/env/manifest.toml`) pins the toolchain via the Nix store instead of container namespaces: Dagger where it works, Flox only where it can't.

Each source is a separate Modal app, so deploying one does not redeploy the others — bump them individually after shared-code changes (e.g. `libs/dlt/`) or stale containers keep importing removed symbols. Never commit the substituted form.

The contract every concrete `src/<source>/webhook/*.py` `Webhook` class must satisfy is `WebhookModelProtocol` in `libs/webhook/protocol.py`, enforced across all sources by `tests/libs/webhook/test_protocol_conformance.py`. Each handler's `TYPE_CHECKING` block aliases `WebhookModelTypeCheckShim` (a `BaseModel` stand-in beside the Protocol) as `WebhookModelToReplace`, so pyright sees both the Pydantic and contract methods in the unsubstituted tree. New sources: implement the existing surface and add a parametrize entry to the conformance test; extend `protocol.py` only for a genuinely new contract method.

**Validate webhook models against a real captured payload, not hand-authored fixtures.** A synthetic cal.com fixture (`start`/`end`/`hosts`) diverged from the real v2 shape (`startTime`/`endTime`/`organizer`), so every test passed while live `BOOKING_CREATED` events 422'd in production, silently. Capture a redacted real payload as a fixture and cross-check field names and the trigger list against cal.com's webhook reference (<https://cal.com/docs/developing/guides/automation/webhooks>) — it defines many triggers we don't handle yet.

### Scripted deploy pitfalls

These explain why `scripts/webhooks-handlers-redeploy.py` is shaped the way it is. The first version was bash; the Python rewrite preserves every mitigation as an explicit module-level idiom. Keep them as design rationale for anyone touching the script:

- **Shebang is plain `python3`, not `uv run python`, plus a self-resolving bootstrap.** `[tool.uv] required-version` makes *any* incompatible `uv` refuse before Python starts, so `uv run python` can't survive a pyenv shim shadowing a compatible install earlier on PATH. `_bootstrap_uv()` scans all of PATH via `scripts/lib/uv_resolve.py` and `os.execv`s into a compatible `uv`; its docstring carries the gating rationale and the caveat that an explicitly-typed `uv run scripts/... ` is not covered.
- **`os.environ.pop("MODAL_TOKEN_ID"/"MODAL_TOKEN_SECRET")` before the Modal-secret preflight.** Infisical injection *wins* over the parent shell — the inverse claim sat here for a while with a test stub built to match it. The pop still matters for `_preflight_modal_secrets`, which reaches Modal through `infisical run`: a leaked personal token there lists the wrong workspace's secrets. The deploy itself is immune; `deploy_env()` sets both tokens explicitly.
- **Wrap with `uv run modal deploy`, not bare `modal deploy`.** Bare `modal` runs outside the project venv and can't import `src.*` packages registered in `pyproject.toml` → `ModuleNotFoundError: No module named 'src.fathom'`. Applies to both executors.
- **Use `shutil.copyfile` (always overwrites) for restore.** The bash version needed `\cp -f` to dodge `cp -i` aliases that silently refused the restore. Swapping it for a helper that accepts `exist_ok=False` would resurrect that footgun.
- **Invoke subprocesses with an argv list, never a string; never `shell=True`.** The bash original stored `infisical run --token … --` in a variable, which zsh treated as `argv[0]`, leaking the service token to stderr and shell history.
- **Preflight Modal tokens, Modal secrets, Infisical keys, and GCS buckets before the deploy loop.** A missing `modal.Secret.from_name(...)` aborts after the image build; a missing Infisical key fails on the first Hookdeck event after deploy; a missing GCS bucket aborts at first write. Resolve the Modal token pair **once** in `main()`, not per source: a rotation or transient Infisical failure mid-`--all` would otherwise split one handler's Modal apps across two workspaces. An explicit fetch also turns "key absent from that env" into a clean failure — `infisical run` would inject nothing, succeed, and let Modal fall back to whatever `~/.modal.toml` profile is active. OTEL sink keys are inventoried only; **no executor forwards them**, deliberately (see Telemetry).
- **`atexit` cleanup, gated on `_BACKUP_FRESHLY_WRITTEN`, scoped to the current handler.** Restores the file even if the deploy raised or was Ctrl-C'd. The gate stops an early-failure exit from copying a *stale* backup over a clean worktree. Signal handlers route SIGINT/SIGTERM through `sys.exit` so `atexit` fires at all.
- **Serialize concurrent invocations.** Two terminals can both pass the clean-tree preflight and then race on the handler file and shared `tmp/webhook-deploy-bak/` — one can delete the other's restore source, or pick up its substitution. An atomic `LOCK_DIR.mkdir(exist_ok=False)` is the advisory lock, released from the `atexit` cleanup.
- **Install `git` in the Dagger container before `uv sync --frozen`.** The `uv` base image ships no git, but `pyproject.toml` pins `gtm-linear` to a git rev, so `uv sync` dies with "Git executable not found". Use one combined `apt-get update && apt-get install -y --no-install-recommends git` exec placed *before* the source mount, so it caches on the base image alone; splitting `update` from `install` reuses a stale apt index. The repo is public, so no git credentials are needed. (ai-8h3)
- **`DAGGER_BASE_IMAGE` pins an exact `uv` release, not just a Python tag.** The unversioned `bookworm-slim` tag drifted below the `[tool.uv] required-version` floor and rejected `uv sync --frozen` in-container; the host-side `_bootstrap_uv()` can't help there. Keep the two pins in lockstep.
- **`dagger.dag.set_secret(name, value)` caches `with_exec` on `name`, not the plaintext.** Reusing a name while its value changes (rotated token, different Infisical project) leaves the cache key unchanged, so Dagger silently replays a *prior* `modal deploy` while reporting success. `_content_addressed_secret_name` hashes the value into the name so it always busts. Dagger-only: the Flox executor passes an explicit `env=` to plain host subprocesses, with no such cache to defeat.

### Registry

`gtm webhook sync` regenerates `webhooks/registry.yaml` by joining `modal app list` with the Hookdeck API; run it after any deploy or Hookdeck wiring change, and `gtm webhook list` to inspect the cache. It is gitignored because it holds personal Modal URLs and Hookdeck IDs that don't belong in OSS — see `webhooks/README.md`.

## Workspace setup (Conductor)

`.conductor/settings.toml`'s `setup` is a thin shim: it sets up `~/.conductor-setup.log` and runs `scripts/conductor-workspace-setup.sh`, where all provisioning lives. On Linux cloud sandboxes, `dolt`/`uv`/`infisical`/`gh` and flake-pinned `bd`/`roborev` come from the committed Flox environment (`.flox/env/manifest.toml` + `manifest.lock` pin versions; `flox activate --mode run` puts them on PATH) — edit the manifest via `flox install`/`flox edit`, never by hand-syncing versions. macOS workspaces without Flox fall back to the original curl installers for `dolt`/`infisical`/`bd`/`roborev`. `uv` is the one exception: presence on PATH isn't enough, so the fallback delegates to `scripts/lib/uv_resolve.py` (see "Scripted deploy pitfalls") and installs a pinned `UV_PINNED_VERSION`, kept in lockstep with the manifest's `uv.version`. The sandboxes have no running systemd and no `/dev/fd`; the script creates the `/dev/fd` symlink and starts `nix-daemon` by hand — don't "simplify" those steps away.

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
