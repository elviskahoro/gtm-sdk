# Webhooks

Standalone Modal apps. Each `export_to_*.py` deploys one Modal app per source
via the `WebhookModelToReplace` placeholder pattern documented in
[`CLAUDE.md` → Webhook deploys](../CLAUDE.md).

## Files

- `export_to_attio.py` — per-source app, writes to Attio.
- `export_to_gcp_etl.py` — per-source app, writes the transformed payload to GCS.
- `export_to_gcp_raw.py` — single dev app pointed at `dlthub-devx-test-bucket`. The
  five `dlthub_devx_<source>_<entity>_raw` apps currently in Modal are deployed
  from a substituted variant; bringing this file in line with that pattern is
  tracked by the `is_valid_webhook` coverage audit ticket.
- `registry.yaml` — **gitignored**. Generated locally; see below.

## Registry

`registry.yaml` is the inventory of `(source, handler) → (Modal app, Modal URL,
Hookdeck source / destination / connection IDs)`. It's generated from live
Modal + Hookdeck state; never edit by hand and never commit.

```shell
# Regenerate the registry. Modal tokens come from Infisical; HOOKDECK_API_KEY
# is sourced from the parent ai/gtm-sdk/.env.local because Infisical's value
# for it is empty (and an empty Infisical value would overwrite the host env
# var — surfacing as silently-null hookdeck_*_id fields in the registry).
unset MODAL_TOKEN_ID MODAL_TOKEN_SECRET  # avoid personal-shell tokens winning
set -a && source /Users/elvis/Documents/ai/gtm-sdk/.env.local && set +a
infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \
  --env=dev -- env HOOKDECK_API_KEY="$HOOKDECK_API_KEY" \
  uv run python -m cli.main webhook sync

# Inspect what's there.
uv run python -m cli.main webhook list
```

Optional: set `MODAL_WORKSPACE` to override the Modal subdomain prefix
(default `devx`). Only needed if you redeploy these apps into a different
Modal workspace.

## Rotation procedure

When you redeploy a Modal app, its name (and therefore its URL) stays the same
— Modal app names are deterministic from `WebhookModel.<handler>_get_app_name()`.
The registry usually only needs a refresh:

1. Redeploy via the substitution pattern in CLAUDE.md.
2. Run `gtm webhook sync` to refresh `generated_at`.

If you change the wiring inside Hookdeck (rerouting a source to a different
Modal destination), re-run `sync` after applying the change in the Hookdeck
dashboard so the IDs in the registry match.

## Why this isn't committed

`gtm-sdk` is a public OSS repo. The registry contains personal Modal URLs and
Hookdeck account IDs that don't belong on GitHub — `.gitignore` keeps them out.
The schema (Pydantic models in `cli/webhook/registry.py`) is the public
contract; the data is local.
