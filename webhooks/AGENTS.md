# Webhooks

Rules for working in `webhooks/`. `CLAUDE.md` and `WARP.md` symlink here.
The operator runbook is [`README.md`](README.md); the deploy footgun catalogue
lives in `scripts/webhooks-handlers-redeploy.py`'s docstrings, on the function
that encodes each one.

## Deploying

Each `export_to_*.py` ships one Modal app **per source**, using a
`WebhookModelToReplace` placeholder so the working tree stays source-agnostic.
`modal deploy` on the file as-is fails with
`NameError: WebhookModelToReplace is not defined`.

```shell
set -a && source .env.local && set +a   # once per shell
export INFISICAL_ENV=dev                 # explicit; no default
scripts/webhooks-handlers-redeploy.py export_to_attio CaldotcomBookingWebhook
scripts/webhooks-handlers-redeploy.py export_to_gcp_etl --all
```

- **Never commit the substituted form.** The script substitutes, deploys, and
  restores in one step, with `atexit` cleanup if it raises or is interrupted.
- **Deploying one source does not redeploy the others.** After a shared-code
  change (e.g. `libs/dlt/`), bump each source individually or stale containers
  keep importing removed symbols.
- **`GTM_DEPLOY_VIA_FLOX=1` on Conductor cloud sandboxes** — Dagger's engine
  cannot start there (#284, cause corrected in #443; see `_deploy_via_flox`).
- **The deploy is one recipe, two executors.** Add a step or a credential to
  `deploy_steps()` / `deploy_env()`, **never** to an executor — the two argv
  lists drifted into deploying *different apps* once already, and
  `tests/scripts/test_deploy_webhook_dagger.py` now pins them together.
- **Do not register these in `src/app.py`.** They are standalone Modal apps.
- **A handler image's `uv_pip_install` must list every dependency the handler
  transitively imports.** A package present in the local venv but absent from
  the minimal Modal image crash-loops the container on import, is invisible to
  local tests, and only shows up in `modal app logs`.

## Adding a source

The contract every concrete `src/<source>/webhook/*.py` `Webhook` class must
satisfy is `WebhookModelProtocol` in `libs/webhook/protocol.py`. Implement the
existing surface and add a parametrize entry to
`tests/libs/webhook/test_protocol_conformance.py`; extend `protocol.py` only
for a genuinely new contract method.

**Validate models against a real captured payload, not hand-authored
fixtures.** A synthetic cal.com fixture (`start`/`end`/`hosts`) diverged from
the real v2 shape (`startTime`/`endTime`/`organizer`), so every test passed
while live `BOOKING_CREATED` events 422'd in production, silently. Capture a
redacted real payload and cross-check field names and the trigger list against
the provider's reference — cal.com's, for instance, defines many triggers we
don't handle yet: <https://cal.com/docs/developing/guides/automation/webhooks>

## Registry

`gtm webhook sync` regenerates `registry.yaml` by joining `modal app list` with
the Hookdeck API; run it after any deploy or Hookdeck wiring change, and
`gtm webhook list` to inspect the cache. It is gitignored because it holds
personal Modal URLs and Hookdeck IDs that don't belong in OSS.
