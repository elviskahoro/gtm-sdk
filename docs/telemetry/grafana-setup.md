# Grafana Cloud Telemetry Setup

This guide walks through setting up Grafana Cloud as a telemetry provider for the otel-collector.

## Endpoints

Grafana Cloud exposes a **region-scoped** OTLP gateway:

```text
https://otlp-gateway-prod-<region>.grafana.net/otlp
```

Find your exact URL on the Grafana Cloud **"OpenTelemetry"** (OTLP) config page for
your stack — e.g. `https://otlp-gateway-prod-us-east-3.grafana.net/otlp` (the
collector's hard-coded default) or `https://otlp-gateway-prod-eu-west-2.grafana.net/otlp`.

The collector strips any trailing `/v1/traces` / `/v1/logs` you paste, so either the
base (`.../otlp`) or a full-signal URL works.

## Authentication (Basic, not Bearer)

Unlike the other providers (Dash0, HyperDX, Logfire all use `Bearer`), Grafana
Cloud's OTLP gateway authenticates with HTTP **Basic** auth:

```text
Authorization: Basic base64("<instance_id>:<token>")
```

- **`<instance_id>`** — the numeric **OTLP-gateway instance / user id** shown on the
  Grafana Cloud "OpenTelemetry" config page. This is _not_ the org id embedded in the
  `glc_` token.
- **`<token>`** — a Grafana Cloud **access-policy token** (starts with `glc_`) scoped
  to write metrics/logs/traces.

You do **not** encode this yourself. Set the two raw values as Infisical secrets and
the collector derives the base64 credential (`GRAFANA_OTLP_AUTH`) **at deploy time**
(see `_grafana_basic_auth` / `_collector_secret_payload` in `src/otel_collector.py`),
so the raw `glc_` token never reaches the collector container or the rendered config.

## Setting Up Secrets in Infisical

Add these secrets to your Infisical project (dev environment):

### 1. GRAFANA_INSTANCE_ID

- **Name**: `GRAFANA_INSTANCE_ID`
- **Value**: The numeric OTLP-gateway instance id (e.g. `1718830`)
- **Type**: Secret
- **Environment**: dev

### 2. GRAFANA_API_KEY

- **Name**: `GRAFANA_API_KEY`
- **Value**: Your Grafana Cloud access-policy token (starts with `glc_`)
- **Type**: Secret
- **Environment**: dev

### 3. GRAFANA_OTLP_ENDPOINT (Optional)

- **Name**: `GRAFANA_OTLP_ENDPOINT`
- **Value**: Your regional OTLP gateway, e.g. `https://otlp-gateway-prod-us-east-3.grafana.net/otlp`
- **Type**: Secret
- **Environment**: dev
- Omit to use the hard-coded default (`us-east-3`).

## Setting Secrets via CLI

```bash
# From the gtm-sdk repo root (its own .env.local holds INFISICAL_TOKEN + INFISICAL_PROJECT_ID)
set -a && source .env.local && set +a

# Set each secret
infisical secrets set \
  GRAFANA_INSTANCE_ID "1718830" \
  --env=dev \
  --projectId "$INFISICAL_PROJECT_ID" \
  --token "$INFISICAL_TOKEN"

infisical secrets set \
  GRAFANA_API_KEY "glc_your-token-here" \
  --env=dev \
  --projectId "$INFISICAL_PROJECT_ID" \
  --token "$INFISICAL_TOKEN"

# Optional — only if you are not on the us-east-3 default region
infisical secrets set \
  GRAFANA_OTLP_ENDPOINT "https://otlp-gateway-prod-us-east-3.grafana.net/otlp" \
  --env=dev \
  --projectId "$INFISICAL_PROJECT_ID" \
  --token "$INFISICAL_TOKEN"
```

## Deploying the Collector

Once secrets are set, deploy the otel-collector:

```bash
# From the gtm-sdk repo root
set -a && source .env.local && set +a

# Deploy the collector (it derives GRAFANA_OTLP_AUTH from the raw inputs above)
infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=dev \
    -- uv run modal deploy src/otel_collector.py
```

At deploy time the raw `GRAFANA_INSTANCE_ID` + `GRAFANA_API_KEY` are collapsed into
the pre-encoded `GRAFANA_OTLP_AUTH` Basic credential and embedded in the collector's
secret, so when the container starts Grafana is already configured.

## Verifying Configuration

Check that Grafana was included in the collector config:

```bash
# From gtm-sdk root:
uv run python -c "
from src.otel_collector import build_collector_config
config = build_collector_config()
exporters = config.get('exporters', {})
print('Exporters:', list(exporters.keys()))
if 'otlphttp/grafana' in exporters:
    print('✓ Grafana is configured')
else:
    print('✗ Grafana is NOT configured')
"
```

## Collector Architecture

When the collector is deployed:

```text
App/Webhook/CLI
    ↓ (Modal RPC spawn)
otel-collector function (Modal)
    ↓ (localhost OTLP)
otelcol sidecar (in container)
    ├─→ Dash0 (if configured)
    ├─→ HyperDX (if configured)
    ├─→ Logfire (if configured)
    └─→ Grafana (if configured; Basic auth)
```

The app-side exporter serializes each batch to OTLP protobuf and spawns the collector
function fire-and-forget. The collector function hands it to a localhost otelcol
process, which handles retry, queueing, and fan-out to all providers.

See `libs/telemetry.py` and `src/otel_collector.py` for implementation details.

## Troubleshooting

### Secrets Not Found

```bash
# Verify secrets exist in Infisical (from the gtm-sdk repo root):
set -a && source .env.local && set +a
infisical secrets get GRAFANA_API_KEY --env=dev \
  --projectId "$INFISICAL_PROJECT_ID" \
  --token "$INFISICAL_TOKEN"
```

### 401 / 403 from the Gateway

- Grafana Cloud uses **Basic** auth, not Bearer — a Bearer header returns 401. This is
  handled automatically by the collector; if you see 401s, re-check that
  `GRAFANA_INSTANCE_ID` is the numeric **OTLP-gateway instance id** (not the org id)
  and that the `glc_` token's access policy has write scope for the signals.
- Confirm the endpoint region matches the stack the token belongs to.

### Collector Not Starting

Check the Modal logs for the otel-collector app:

```bash
infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=dev \
    -- uv run modal app logs otel-collector
```

### No Telemetry in Grafana

1. Verify the collector is running and Grafana is in the exporter list (see above).
2. Check Modal logs for export errors.
3. Verify the OTLP endpoint region is reachable and matches the token's stack.

### Mixing Grafana with Other Providers

You can enable multiple providers simultaneously — the collector fans out to all
configured exporters. Grafana coexists with Dash0, HyperDX, and Logfire.

## Reference

- [Grafana Cloud OTLP endpoint docs](https://grafana.com/docs/grafana-cloud/send-data/otlp/send-data-otlp/)
- [OpenTelemetry Collector OTLPHTTP Exporter](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlphttpexporter/README.md)
- `src/otel_collector.py` — Collector app and config builder
- `libs/telemetry.py` — App-side telemetry initialization
