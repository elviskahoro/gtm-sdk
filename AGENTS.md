# AGENTS.md

Rules for working in this repo. `CLAUDE.md` and `WARP.md` symlink here. The repo layout, CLI surface, and adapter list are discoverable — don't expect this file to mirror them.

## Code placement

- `libs/<service>/` — wrap **one** external SDK/API. Idiomatic Python types/functions only.
- `src/` — orchestration. Multi-step flows, side effects, Modal `@app.function` / `@modal.fastapi_endpoint` decorators.
- `cli/` — Typer subapps. Parse → preflight → call `src/` → render. **No business logic.**
- `data_gen/` — independent, composable data products.
- `webhooks/` — standalone Modal apps. Deploy individually with `modal deploy webhooks/<file>.py`. Do **not** register them in `src/app.py`.
- `api/specs/`, `api/samples/` — external API specs and fixture payloads. Read-only reference.
- `tmp/` — scratch only. Gitignored. Never write temp files anywhere else.

### Hard rules

- **No cross-lib imports.** `libs/<x>` must not import from `libs/<y>`. If two adapters need to coordinate, do it in `src/`.
- **No orchestration in `libs/`.** Adapter modules must be callable in isolation.
- **New top-level package?** Update `[tool.setuptools.packages.find]` in `pyproject.toml` (currently `cli*`, `data_gen*`, `libs*`, `src*`).

## Modal gotchas

- `deploy.py` stays at the repo root. Moving it under `src/` causes `src/attio/` to shadow the `attio` pip package.
- New endpoint = add the module import to `_ENDPOINT_MODULES` in `src/app.py`, otherwise its decorators don't register.
- New secret = add a `modal.Secret.from_name("<name>")` binding in `src/app.py`.
- Free tier caps the app at **8 web endpoints**. Don't silently exceed it.
- App name resolves from the `MODAL_APP` env var (`src/modal_app.py`).

## Telemetry

OTEL via `libs/telemetry.py`. Activated only when one of these is set: `HYPERDX_API_KEY`, `HYPERDX_OTLP_ENDPOINT`, or `OTEL_EXPORTER_OTLP_ENDPOINT`. Otherwise the tracer is a no-op — don't add fallback logging "just in case."

## Secrets (Infisical)

`.env.local` at the repo root holds `INFISICAL_TOKEN` and `INFISICAL_PROJECT_ID`. There is no `.infisical.json`, so the CLI does not auto-detect the project — pass flags explicitly or source the env file first:

```shell
set -a && source .env.local && set +a
infisical secrets --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=dev
infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=dev -- <cmd>
```

Conductor workspaces get `.env.local` copied in at provisioning; the parent `ai/` repo's `.env*` files are not copied. Never fall back to 1Password unless the user explicitly asks.

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
- Significant changes → `CHANGELOG.md`.
- Architectural decisions → design artifacts in the parent `ai/` repo's `design/`, not loose `.md` here.

If you finish a task and want to summarize, output it as your final response. Don't write a file.

## Git

- **Branches**: `agent/<slug>`. Never `claude/*` or other provider-specific prefixes.
- **Worktrees**: `worktrees/<branch-name>` at the repo root. Ensure `worktrees/` exists and is gitignored. Never use `.git/modules/*` paths as user-facing worktree locations.
- **Commits/PRs**: never add AI co-author trailers (`Co-Authored-By: Claude/Oz/...`). Human authors only.

## Testing

`uv run pytest`. Importlib mode is already configured. Mirror the source layout when adding tests.
