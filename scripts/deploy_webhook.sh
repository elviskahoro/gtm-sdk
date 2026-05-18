#!/usr/bin/env bash
# trunk-ignore-all(shellcheck/SC2310): in_array is a pure boolean check; intentional 0/1 return
# trunk-ignore-all(shellcheck/SC2312): discovery pipelines feed `mapfile < <(...)` where pipe exit codes are intentionally ignored — empty output is handled explicitly downstream
#
# scripts/deploy_webhook.sh — substitute, deploy, and restore a webhook
# handler in one safe step. Encodes every footgun catalogued in AGENTS.md
# "Scripted deploy pitfalls" so callers don't have to remember them.
#
# Usage:
#   scripts/deploy_webhook.sh <handler> <source>
#   scripts/deploy_webhook.sh <handler> --all
#
# <handler>: export_to_attio | export_to_gcp_etl
# <source> : CaldotcomBookingWebhook | FathomCallWebhook | FathomMessageWebhook
#            | OctolensMentionWebhook | Rb2bVisitWebhook
#
# What it does:
#   1. Refuses to start if INFISICAL_PROJECT_ID or INFISICAL_TOKEN is unset.
#   2. Unsets MODAL_TOKEN_ID / MODAL_TOKEN_SECRET so infisical-injected
#      dlthub workspace tokens win over the parent shell's personal tokens.
#   3. Refuses to start if the working tree under webhooks/ is dirty.
#   4. Preflights that required Modal secrets exist before any file edit.
#   5. Backs up the handler to tmp/webhook-deploy-bak/, sed-substitutes
#      WebhookModelToReplace → <source>, deploys via `infisical run -- uv
#      run modal deploy`, then restores from the backup.
#   6. A trap on EXIT runs the restore even if the script dies mid-deploy
#      (Ctrl-C, modal failure, etc.) — the working tree always ends clean.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "${REPO_ROOT}"

BACKUP_DIR="tmp/webhook-deploy-bak"

# Discover valid handlers: any .py file in webhooks/ that uses the
# WebhookModelToReplace placeholder pattern. New handlers (e.g. when
# export_to_gcp_raw adopts the placeholder pattern) are picked up
# automatically — no edit to this script required.
mapfile -t VALID_HANDLERS < <(
  grep -l 'WebhookModelToReplace' webhooks/*.py 2>/dev/null |
    xargs -n1 -I{} basename {} .py
)
[[ ${#VALID_HANDLERS[@]} -gt 0 ]] || {
  echo "ERROR: no webhook handlers under webhooks/ contain WebhookModelToReplace." >&2
  exit 1
}

# VALID_SOURCES is discovered per-handler in discover_valid_sources(), below.
VALID_SOURCES=()

usage() {
  cat >&2 <<EOF
Usage:
  scripts/deploy_webhook.sh <handler> <source>
  scripts/deploy_webhook.sh <handler> --all

  <handler>  : ${VALID_HANDLERS[*]}
  <source>   : any 'Webhook as <Alias>' alias imported by <handler>.py
               (discovered automatically from the handler's import block)
  --all      : deploy every source imported by the chosen handler

Preconditions:
  - INFISICAL_PROJECT_ID and INFISICAL_TOKEN exported in env
    (run: set -a && source .env.local && set +a)
  - working tree under webhooks/ is clean
  - required Modal secrets exist in the dlthub workspace
EOF
}

# Parse the handler's `Webhook as <Alias>` import lines and populate
# VALID_SOURCES. Matches the canonical import shape used by every
# placeholder-pattern handler:
#
#     from src.<pkg>.webhook.<mod> import (
#         Webhook as <SomeWebhook>,
#     )
#
# Single source of truth: the imports themselves. If a handler adds or
# drops a source, the script tracks it on the next invocation.
discover_valid_sources() {
  local handler_file="$1"
  mapfile -t VALID_SOURCES < <(
    grep -E '^[[:space:]]*Webhook as [A-Za-z_][A-Za-z0-9_]*,?[[:space:]]*$' \
      "${handler_file}" |
      sed -E 's/^[[:space:]]*Webhook as ([A-Za-z_][A-Za-z0-9_]*),?[[:space:]]*$/\1/'
  )
  [[ ${#VALID_SOURCES[@]} -gt 0 ]] || fail "No 'Webhook as <Alias>' imports found in ${handler_file}."
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

in_array() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    [[ ${item} == "${needle}" ]] && return 0
  done
  return 1
}

# Restore every backed-up handler from tmp/webhook-deploy-bak/ to webhooks/.
# Uses \cp -f to bypass any cp -i alias the user might have set; without
# the backslash, an interactive cp would silently answer "no" and leave
# the substituted form in place — the classic pitfall this script exists
# to prevent.
restore_all() {
  if [[ -d ${BACKUP_DIR} ]]; then
    local backup
    for backup in "${BACKUP_DIR}"/*.py; do
      [[ -e ${backup} ]] || continue
      \cp -f "${backup}" "webhooks/$(basename "${backup}")"
    done
  fi
}

# --- argument parsing -------------------------------------------------------

if [[ $# -ne 2 ]]; then
  usage
  exit 2
fi

HANDLER="$1"
SOURCE_OR_ALL="$2"

if ! in_array "${HANDLER}" "${VALID_HANDLERS[@]}"; then
  usage
  fail "Unknown handler: ${HANDLER}"
fi

HANDLER_FILE="webhooks/${HANDLER}.py"
[[ -f ${HANDLER_FILE} ]] || fail "Handler file not found: ${HANDLER_FILE}"

discover_valid_sources "${HANDLER_FILE}"

if [[ ${SOURCE_OR_ALL} == "--all" ]]; then
  SOURCES_TO_DEPLOY=("${VALID_SOURCES[@]}")
elif in_array "${SOURCE_OR_ALL}" "${VALID_SOURCES[@]}"; then
  SOURCES_TO_DEPLOY=("${SOURCE_OR_ALL}")
else
  usage
  echo "ERROR: Unknown source: ${SOURCE_OR_ALL}" >&2
  echo "  Sources imported by ${HANDLER_FILE}: ${VALID_SOURCES[*]}" >&2
  exit 1
fi

# --- preflight: env ---------------------------------------------------------

[[ -n ${INFISICAL_PROJECT_ID-} ]] || fail "INFISICAL_PROJECT_ID is unset. Run: set -a && source .env.local && set +a"
[[ -n ${INFISICAL_TOKEN-} ]] || fail "INFISICAL_TOKEN is unset. Run: set -a && source .env.local && set +a"

# The parent shell's personal Modal tokens silently override infisical-injected
# workspace tokens — deploys land in the wrong workspace. Always unset.
unset MODAL_TOKEN_ID MODAL_TOKEN_SECRET

# --- preflight: working tree -----------------------------------------------

# `git diff --quiet` only compares the worktree against the index, so a
# staged-but-uncommitted edit would slip past. `git status --porcelain` flags
# any deviation from HEAD: staged, unstaged, or untracked.
WEBHOOKS_STATUS="$(git status --porcelain -- webhooks/)"
if [[ -n ${WEBHOOKS_STATUS} ]]; then
  echo "ERROR: webhooks/ has uncommitted changes (staged, unstaged, or untracked). Aborting." >&2
  echo "${WEBHOOKS_STATUS}" >&2
  exit 1
fi

# --- preflight: Modal secrets ----------------------------------------------

# All five sources currently return ["devx-gcp-202605111323"] from
# modal_get_secret_collection_names() and ["attio"] from
# attio_get_secret_collection_names(). If a new source introduces a
# different secret, extend this list.
REQUIRED_SECRETS=(devx-gcp-202605111323)
if [[ ${HANDLER} == "export_to_attio" ]]; then
  REQUIRED_SECRETS+=(attio)
fi

echo "Preflighting Modal secrets: ${REQUIRED_SECRETS[*]}"
# Use --json so secret names are emitted in full. The default table renderer
# truncates the Name column with `…`, which made `grep -w` miss long secret
# names like `devx-gcp-202605111323` even when they exist.
MODAL_SECRET_LIST="$(
  infisical run \
    --projectId "${INFISICAL_PROJECT_ID}" \
    --token "${INFISICAL_TOKEN}" \
    --env=dev \
    -- uv run modal secret list --json 2>/dev/null
)" || fail "Could not list Modal secrets — check Infisical token and Modal access."

for secret in "${REQUIRED_SECRETS[@]}"; do
  # Match `"Name": "<secret>"` exactly so a prefix like `devx-gcp` doesn't
  # accidentally satisfy a check for `devx-gcp-202605111323`.
  if ! grep -qF "\"Name\": \"${secret}\"" <<<"${MODAL_SECRET_LIST}"; then
    fail "Missing Modal secret in dlthub workspace: ${secret}. Create it before deploying."
  fi
done

# --- preflight: GCS buckets (handlers that write to gs://) -----------------

# Auto-detect whether the handler routes to a per-source GCS bucket by
# grepping for `WebhookModel.<prefix>_get_bucket_name`. The etl handler
# uses `etl_get_bucket_name`; the (future) raw handler will use
# `raw_get_bucket_name`. The attio handler doesn't write to GCS, so this
# pattern is absent and the preflight is skipped.
#
# This matches AGENTS.md "Scripted deploy pitfalls": a missing bucket aborts
# at first webhook write — a deployed-but-broken endpoint. Catching it here
# means no Modal app gets created against a bucket that doesn't exist.
# Use a bash regex loop rather than `grep | head | sed`: with `set -o
# pipefail`, an empty grep result (attio handler — no bucket) propagates
# exit code 1 through the pipe and trips `set -e`. The loop returns an
# empty string cleanly when no match is found.
BUCKET_METHOD=""
while IFS= read -r _line; do
  if [[ ${_line} =~ WebhookModel\.([a-z_]+_get_bucket_name) ]]; then
    BUCKET_METHOD="${BASH_REMATCH[1]}"
    break
  fi
done <"${HANDLER_FILE}"

# Map `<SourceClass>` to its module path by parsing the handler's import
# block. Each source is imported via the canonical shape:
#   from src.<pkg>.webhook.<mod> import (
#       Webhook as <SourceClass>,
#   )
# awk walks the file and tracks the most recent `from src…` line; when it
# sees `Webhook as <source>` it strips `from ` and ` import (` and emits the
# dotted module path.
source_module_for() {
  local handler_file="$1" source="$2"
  awk -v target="Webhook as ${source}" '
    /^from src\.[A-Za-z0-9_.]+ import \(/ { last_from = $0 }
    index($0, target) {
      sub(/^from /, "", last_from)
      sub(/ import \(.*$/, "", last_from)
      print last_from
      exit
    }
  ' "${handler_file}"
}

if [[ -n ${BUCKET_METHOD} ]]; then
  command -v gcloud >/dev/null 2>&1 || fail "gcloud CLI not found — required to preflight GCS buckets for ${HANDLER}."
  echo "Preflighting GCS buckets via WebhookModel.${BUCKET_METHOD}()"
  for source in "${SOURCES_TO_DEPLOY[@]}"; do
    module="$(source_module_for "${HANDLER_FILE}" "${source}")"
    [[ -n ${module} ]] || fail "Could not resolve module path for ${source} in ${HANDLER_FILE}."

    # Pure static method on the Webhook subclass — no env / secrets needed,
    # so we don't pay the infisical-injection round-trip here.
    bucket="$(uv run python -c "from ${module} import Webhook; print(Webhook.${BUCKET_METHOD}())" 2>/dev/null)" ||
      fail "Could not resolve bucket name for ${source} via ${module}.Webhook.${BUCKET_METHOD}()."

    if ! gcloud storage ls --project=dlthub-sandbox "gs://${bucket}" >/dev/null 2>&1; then
      fail "Missing GCS bucket: gs://${bucket} (source ${source}). Create it before deploying."
    fi
    echo "  ${source}: gs://${bucket} ✓"
  done
fi

# --- backup + trap (in that order, so trap never sees stale state) ---------

# Clear any stale backups from prior runs *before* taking the fresh one.
# Otherwise a leftover backup from an earlier session — possibly for a
# different handler entirely — would be used as the restore source, leaving
# the worktree dirty after restore (and the EXIT trap could clobber an
# unrelated webhooks/ file with that stale content).
#
# Safe because the working-tree preflight above guarantees webhooks/ matches
# HEAD; the new backup we're about to write is the committed form.
#
# The trap is registered *after* the fresh backup is in place, so even if a
# later step fails before the first sed, the restore is a no-op against an
# already-clean tree. Before this point, any failure leaves webhooks/
# untouched, so no restore is needed.
mkdir -p "${BACKUP_DIR}"
rm -f "${BACKUP_DIR}"/*.py
\cp -f "${HANDLER_FILE}" "${BACKUP_DIR}/${HANDLER}.py"

trap 'restore_all' EXIT

# --- deploy loop -----------------------------------------------------------

deploy_one() {
  local source="$1"

  echo
  echo "=== Deploying ${source} via ${HANDLER} ==="

  # Substitute placeholder → concrete source class.
  # sed -i.bak is portable across GNU and BSD sed; we delete the .bak after.
  sed -i.bak "s/WebhookModelToReplace/${source}/g" "${HANDLER_FILE}"
  rm -f "${HANDLER_FILE}.bak"

  # `uv run modal deploy` (not bare `modal deploy`) — bare modal runs
  # outside the project venv and can't import `src.*` packages.
  infisical run \
    --projectId "${INFISICAL_PROJECT_ID}" \
    --token "${INFISICAL_TOKEN}" \
    --env=dev \
    -- uv run modal deploy "${HANDLER_FILE}"

  # Restore from the backup so the next iteration starts from a clean
  # placeholder state.
  \cp -f "${BACKUP_DIR}/${HANDLER}.py" "${HANDLER_FILE}"

  # Verify the restore actually worked. If a `cp -i` alias somehow won
  # over the backslash form (shell function shadowing, etc.), the tree
  # would still be dirty here — fail loudly with the diff dumped. Uses
  # porcelain to also catch the staged-edit case (matches preflight).
  local post_restore_status
  post_restore_status="$(git status --porcelain -- "${HANDLER_FILE}")"
  if [[ -n ${post_restore_status} ]]; then
    echo "ERROR: ${HANDLER_FILE} is dirty after restore — placeholder swap failed." >&2
    echo "${post_restore_status}" >&2
    git diff HEAD -- "${HANDLER_FILE}" >&2
    exit 1
  fi
}

for source in "${SOURCES_TO_DEPLOY[@]}"; do
  deploy_one "${source}"
done

echo
echo "All deploys complete. Working tree clean."
