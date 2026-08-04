# AGENTS.md

Rules for working in this repo. `CLAUDE.md` and `WARP.md` symlink here.

This file is deliberately short. Deep rationale lives beside the code it
governs — read the docstring or config comment at the point of use rather than
expecting a copy here. Directory-scoped rules live in `<dir>/AGENTS.md`
(`webhooks/`, `docs/`, `tests/`); read those when you work there. The repo
layout, CLI surface, and adapter list are discoverable — don't expect this file
to mirror them.

## Code placement

- `libs/<service>/` — wrap **one** external SDK/API. Idiomatic Python types/functions only.
- `src/` — orchestration. Multi-step flows, side effects, Modal `@app.function` / `@modal.fastapi_endpoint` decorators.
- `cli/` — All CLI command surfaces use Typer subapps. Parse → preflight → call `src/` → render. **No business logic.**
- `data-gen/` — independent, composable data products.
- `webhooks/` — standalone Modal apps. See `webhooks/AGENTS.md`.
- `api/specs/`, `api/samples/` — external API specs and fixture payloads. Read-only reference.
- `tmp/` — scratch only. Gitignored. Never write temp files anywhere else.

### Hard rules

- **No cross-lib imports.** `libs/<x>` must not import from `libs/<y>`. If two adapters need to coordinate, do it in `src/`. Exceptions: utilities (`libs.telemetry`, `libs.logging`, `libs.filesystem`) are importable from anywhere.
- **No orchestration in `libs/`.** Adapter modules must be callable in isolation.
- **Module boundaries are enforced by tach**, run via trunk like every other linter. Reproduce with `trunk check --filter=tach`; CI runs `--all` on every push and PR. Config is max-strict for this repo: `exact`, `root_module = "forbid"`, layered modules with `layers_explicit_depends_on`, frozen `[[interfaces]]`, `visibility`, and `TYPE_CHECKING`/string imports enforced. Bump the `dev`-group `tach` pin in `pyproject.toml` in lockstep with the `tach@` version in `.trunk/trunk.yaml` and the plugin `ref` in `oss-linter-trunk-tach`.
- **New top-level package?** Update `[tool.setuptools.packages.find]` in `pyproject.toml`, and declare the module (plus `depends_on` / `[[interfaces]]` as needed) in `tach.toml` — or `uv run tach sync --add` for depends_on.

## Public API and downstream consumers

**This repo is a library. Unreferenced here ≠ dead.** Other repos install
gtm-sdk as an editable path dep, so their call sites are invisible to this
repo's tests, to `ruff`, and to any dead-code scanner. Two mechanisms make the
real surface visible, and neither is optional:

- **`libs/<x>/__init__.py`'s `__all__` is the public-API declaration.** Dead-code tooling treats an `__all__` re-export as an entrypoint. Removing a name from one is a breaking change, not cleanup.
- **`contracts/downstream_api.toml` records what consumers actually import**, enforced by `tests/test_downstream_contract.py` in the required `Unit tests` gate. It also pins the few private helpers consumers reach into, which stay out of `__all__` on purpose.

**Before merging any dead-code PR, grep both.** If the symbol appears in
either, close the PR. If it is genuinely consumed but absent from the contract,
that is a contract bug — fix it first. The test's docstring carries the
incident that motivated all of this.

Consumer imports changed, or you added a consumer? Regenerate rather than
hand-edit — `scripts/downstream-contract-sync.py <consumer-path> --write`. It
is deliberately **not** in CI: this repo is public and the consumer tree is
private, so the contract travels as committed data instead.

## Modal gotchas

- `deploy.py` stays at the repo root. Moving it under `src/` causes `src/attio/` to shadow the `attio` pip package.
- New endpoint = add the module import to `_ENDPOINT_MODULES` in `src/app.py`, otherwise its decorators don't register.
- New secret = add `"<X>_API_KEY": <x>_client.api_key_scope` to `KEY_SCOPES` in `src/secrets_bootstrap.py` (after wiring an `api_key_scope` contextvar in `libs/<x>/client.py`), then decorate with `@with_secrets("<X>_API_KEY")` and bind `secrets=[bootstrap_secret()]`. Do NOT use `modal.Secret.from_name(...)` — see ai-672.
- Free tier caps the app at **8 web endpoints**. Don't silently exceed it.
- App name resolves from the `MODAL_APP` env var (`src/modal_app.py`).
- **Troubleshoot a deployed app via the CLI** — `infisical run … -- uv run modal app logs <app-name>`. A crash-looping container prints its import traceback there.

## Telemetry

OTEL via `libs/telemetry.py`, two modes: collector fan-out (the default, and the
only path that reaches Logfire) and a direct single-sink fallback. The block
comment above `DEFAULT_COLLECTOR_APP` explains both and the traps; the
user-facing version is `docs/telemetry/overview.mdx`. Neither configured → no-op.
**Don't add fallback logging "just in case."**

## Secrets (Infisical)

`.env.local` at the repo root holds `INFISICAL_TOKEN` and `INFISICAL_PROJECT_ID`.
There is no `.infisical.json`, so the CLI does not auto-detect the project —
pass flags explicitly or source the env file first:

```shell
set -a && source .env.local && set +a
infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=dev -- <cmd>
```

Conductor workspaces get `.env.local` copied in at provisioning; the parent
`ai/` repo's `.env*` files are not. Never fall back to 1Password unless asked.

`gh` reads `GH_TOKEN`/`GITHUB_TOKEN` straight from the environment — no
`gh auth login`, and no browser flow works in a headless sandbox. It is a
personal PAT, not a shared team secret, so it does **not** go through Infisical:
set it under `[environment_variables]` in user-level Conductor settings or
another ignored local file. The tracked `.conductor/settings.local.toml` is a
reviewed baseline and must not contain credentials.

## Workspace setup (Conductor)

`.conductor/settings.toml`'s `setup` is a thin shim; all provisioning lives in
`scripts/conductor-workspace-setup.sh`, whose comments carry the reasoning for
each step. Toolchain versions come from the committed Flox environment
(`.flox/env/manifest.toml`) — edit it via `flox install`/`flox edit`, never by
hand-syncing versions.

`bd`/`roborev` on **aarch64-darwin** resolve from FloxHub packages
(`elvis/bd`, `elvis/roborev`) repackaged from upstream release binaries via
`[build.bd]`/`[build.roborev]` in the manifest — this avoids the Nix-sandbox
purity failures a flake source build hits on a sandbox with no working Nix
sandbox (gtm-sdk#445). Version bump: bump the pinned asset version + sha256
in `[build.*]` → commit + push (publish clones from the remote) → `flox
build <name>` → verify the tool's exact CLI surface against what
`conductor-workspace-setup.sh` invokes → `flox publish <name>` → bump the
`version` in `[install]` → regenerate `manifest.lock` via `flox` (never
hand-edit) → commit. **aarch64-linux/x86_64-linux are not published yet**
(no Linux builder available) — those systems still resolve `bd`/`roborev`
via the original flake pins (`bd-linux`/`roborev-linux` in `[install]`),
unchanged from before.

Because the FloxHub catalog is private by default, `conductor-workspace-setup.sh`
authenticates via `flox auth login --token-file` before activation when a
`FLOXHUB_TOKEN` is available (`.env.local`, same convention as
`DOLTHUB_CREDENTIAL`; Infisical wiring is a follow-up). A missing token
isn't fatal — activation still runs and fails closed into the existing
curl-fallback path.

## Package management

**Use `uv`. Never `pip`, `pip3`, or `python3 -m pip`.** Bare pip bypasses
`uv.lock` and causes environment drift. `uv sync` installs from lock,
`uv add <pkg>` records a project dependency in `pyproject.toml`/`uv.lock`
(`uv pip install <pkg>` only installs into the active env, for disposable
setups — it never touches either file), `uv run <cmd>` runs inside the env.

## Path anchoring

When a script reads/writes files that live beside it, anchor on
`Path(__file__).resolve().parent`, not the CWD. `uv run path/to/script.py` does
**not** chdir, so relative paths silently resolve to the wrong place.

## Script entrypoints

- Scripts meant to run under `infisical run -- <cmd>` should be directly executable and use a uv shebang when practical. If one cannot be, say why in the usage text.
- Put the canonical Infisical example string in `scripts/lib/env.py` and reuse it from docstrings and error messages instead of hand-writing variants.
- Usage examples must show `--projectId`, `--token`, and `--env`, or explicitly say the script depends on `infisical init`.
- Scripts that need an isolated toolchain run one recipe under either of two executors — Dagger by default, Flox where no Dagger engine can start (Conductor cloud sandboxes). Each selects via its own `GTM_*_VIA_FLOX` flag through `env_flag()`: `webhooks-handlers-redeploy.py`, `pr-review-threads.py`, `hookdeck-connection_events-dump.py`. Add a step to the recipe, never to one executor; each script's docstring carries its specifics.
- **New scripts default to Python.** The only standing shell exceptions are `scripts/conductor-workspace-setup.sh` (bootstraps the toolchain before a Python interpreter is guaranteed to exist, and Dagger's engine cannot start in a Conductor cloud sandbox — see the script's own header) and `scripts/git-hooks/{prepare-commit-msg,commit-msg}` (git invokes hook files directly and synchronously on every commit, so they can't assume a `uv` venv or a Dagger container is available). Don't add a fourth without the same kind of structural reason.

## Linting

All linters/formatters run via **trunk**, not as bare binaries — `ruff`,
`mypy`, `yamllint`, `shellcheck`, `bandit`, `actionlint` and friends live in
trunk's sandbox, so invoking them directly will `command not found` or use the
wrong config. Reproduce a finding with `trunk check --filter=<tool> <path>`;
format with `trunk fmt <path>`.

## Testing

`uv run pytest`. Importlib mode is already configured. Mirror the source layout
when adding tests. See `tests/AGENTS.md` before adding a pytest plugin or a
property-based test.

## Documentation

**Do not create summary, investigation, or "what I did" `.md` files.** If you
finish a task and want to summarize, output it as your final response.

- Docstrings explain *why*, not *what*. Comments document decisions and gotchas inline.
- Notable releases → `docs/changelog/`. Architectural decisions → design artifacts in the parent `ai/` repo, not loose `.md` here.
- **Never hand-mirror enumerable inventories** (adapter tables, CLI trees, endpoint lists) — they rot. Point at `uv run gtm --help` / `ls libs/` and the docs site instead.
- Editing the published site? See `docs/AGENTS.md`.

## Git

- **Branches**: `agent/<slug>`. Never `claude/*` or other provider-specific prefixes.
  - **Exception — Linear-initiated branches:** these are pre-created with a `feature/` prefix and Linear's ticket ↔ branch linkage depends on the original name. Check `git branch --show-current` when picking up a handoff; if it already starts with `feature/`, keep committing to it.
- **Worktrees**: `worktrees/<branch-name>` at the repo root, gitignored. Never use `.git/modules/*` paths as user-facing worktree locations.
- **Commits/PRs**: never add AI co-author trailers (`Co-Authored-By: Claude/Oz/...`). Human authors only.

## Issue tracking

**When handed a bead prompt, claim it FIRST.** If the task references a bead ID
(e.g. `○ ai-5ph ● P2 hermes: deploy hermes-agent`), run `bd update <id> --claim`
and `bd update <id> --status in_progress` before doing any other work. The rest
of the beads workflow is in the generated block at the end of this file.

## Session Completion

**When ending a work session:** file issues for anything left over, run the
quality gates if code changed, close or update the issues you touched, then
commit locally with a clear message. Added or renamed a CLI command, adapter,
endpoint, or webhook handler? Run
`uv run scripts/docs-cli_reference-generate.py` and grep README/AGENTS/docs for
the old name. Clean up stashes and stale remote branches once a push is
authorized, and hand off context for the next session.

Pushing is a shared-state action — committing locally is reversible, pushing is
not, so match the action to the blast radius:

- **Roborev gate (applies to ALL branches):** never `git push` to origin without running `git roborev review --wait` against HEAD first and confirming a clean review. If roborev is unavailable or fails to run, say so and ask before pushing.
- On `agent/*`, `feature/*`, or `conductor/*` branches: once the roborev gate passes, you MAY `git pull --rebase && git push` without asking — these are scratch branches owned by the current task.
- On `main`, `master`, or any release/protected branch: **DO NOT push without explicit user confirmation.** Stop after the commit, say what would be pushed, and ask. If unsure which category a branch falls into, treat it as protected.
- **NEVER force-push without an explicit ask**, regardless of branch. If a push fails, surface the error and ask before resolving — do not retry in a loop or rewrite history.

<!-- Everything below is generated by `bd` and rewritten wholesale by `bd init` /
     `bd setup`. Repo policy lives ABOVE this marker on purpose: it used to sit
     inside the block, where a bd upgrade would have silently replaced the push
     policy with bd's own conflicting version. Keep it that way, and don't
     hand-edit the hash. Provisioning passes --skip-agents (see
     scripts/conductor-workspace-setup.sh). -->
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

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.
<!-- END BEADS INTEGRATION -->
