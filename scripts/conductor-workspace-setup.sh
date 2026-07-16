#!/usr/bin/env bash
# Conductor workspace provisioning. Invoked by .conductor/settings.toml's
# `setup` shim (which owns the log redirect); safe to re-run — every step is
# idempotent. Tool provisioning goes through the committed Flox environment
# (.flox/) on Linux sandboxes; on macOS (or anywhere Flox is absent) it falls
# back to the original curl-installer path so local Mac workspaces keep
# working unchanged.
#
# Hard constraint: NO process substitution (`<(...)`) anywhere in this script.
# Conductor cloud sandboxes lack /dev/fd until we create it below, and with
# `set -e` an unopenable process-substitution fd kills the script silently
# (see commit 585b008 and issue #279).
# shellcheck disable=SC2312  # $(uname)/$(git ...) in assignments: a failure there should (and does) abort via set -e
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- Parent-repo symlinks -------------------------------------------------
PARENT_REPO="$(dirname "$(git rev-parse --git-common-dir)")/.."
PRIMARY_REPO_ROOT="$(cd "${PARENT_REPO}" && pwd)"

[[ ! -f .env.local ]] && [[ -f "${PRIMARY_REPO_ROOT}/.env.local" ]] && ln -s "${PRIMARY_REPO_ROOT}/.env.local" .env.local
[[ ! -L .agents ]] && [[ -d "${PRIMARY_REPO_ROOT}/.agents" ]] && ln -s "${PRIMARY_REPO_ROOT}/.agents" .agents
[[ ! -L .claude ]] && [[ -d "${PRIMARY_REPO_ROOT}/.claude" ]] && ln -s "${PRIMARY_REPO_ROOT}/.claude" .claude

export PATH="${HOME}/.local/bin:${PATH}"

# --- Flox bootstrap (Linux cloud sandboxes only) ---------------------------
# Flox = Nix under the hood: declarative manifest (.flox/env/manifest.toml),
# lockfile-pinned versions, binary-cache installs. Chosen over Dagger for
# setup tooling because Dagger's engine requires a privileged containerized
# BuildKit/runc stack that cannot run in these sandboxes (issue #284).
#
# macOS: never install Flox unattended (needs Homebrew or an interactive
# .pkg). If a Mac already has flox on PATH we use it; otherwise the curl
# fallbacks below preserve today's behavior.
if [[ "$(uname -s)" == "Linux" ]] && command -v dnf >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  # Vercel sandboxes ship no /dev/fd; flox's activate helpers (and any bash
  # process substitution) need it. /proc/self/fd is the canonical target.
  [[ -e /dev/fd ]] || sudo ln -sfn /proc/self/fd /dev/fd

  if ! command -v flox >/dev/null 2>&1; then
    # Unversioned stable-channel RPM; xz is an undeclared runtime dep of the
    # install scriptlets on minimal AL2023 images.
    sudo dnf install -y xz >/dev/null
    curl -fsSLo /tmp/flox.rpm https://downloads.flox.dev/by-env/stable/rpm/flox.x86_64-linux.rpm
    sudo rpm --import https://downloads.flox.dev/by-env/stable/rpm/flox-archive-keyring.asc
    sudo rpm -ivh /tmp/flox.rpm
    rm -f /tmp/flox.rpm
  fi
  flox --version

  # Flox uses multi-user Nix. These sandboxes have systemd installed but
  # offline (PID 1 is sandbox-init), so nix-daemon.socket never activates —
  # start the daemon by hand when its socket is absent. Guard also lands in
  # ~/.bashrc so later shells self-heal after a daemon death.
  NIX_DAEMON_GUARD='[ -S /nix/var/nix/daemon-socket/socket ] || sudo -bn /usr/sbin/nix-daemon --daemon >/dev/null 2>&1 || true'
  if [[ ! -S /nix/var/nix/daemon-socket/socket ]]; then
    # shellcheck disable=SC2024  # the log is meant to be user-owned; only the daemon needs root
    sudo -b /usr/sbin/nix-daemon --daemon >/tmp/nix-daemon.log 2>&1
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      [[ -S /nix/var/nix/daemon-socket/socket ]] && break
      sleep 1
    done
    [[ -S /nix/var/nix/daemon-socket/socket ]] || {
      echo "error: nix-daemon socket never appeared (see /tmp/nix-daemon.log)"
      exit 1
    }
  fi
  if ! grep -qF "${NIX_DAEMON_GUARD}" "${HOME}/.bashrc" 2>/dev/null; then
    printf '\n# gtm-sdk conductor setup: keep nix-daemon alive for flox (no systemd)\n%s\n' "${NIX_DAEMON_GUARD}" >>"${HOME}/.bashrc"
  fi
fi

# --- Tool provisioning ------------------------------------------------------
FLOX_TOOLS_VERIFIED=0
FLOX_FAILURE_STAGE=""

verify_flox_tool() {
  local tool="$1"
  shift
  local tool_path

  tool_path="$(command -v "${tool}" || true)"
  case "${tool_path}" in
    "${FLOX_BIN}"/*) ;;
    "")
      echo "error: Flox tool '${tool}' is missing from ${FLOX_BIN}" >&2
      return 1
      ;;
    *)
      echo "error: Flox tool '${tool}' resolved outside ${FLOX_BIN}: ${tool_path}" >&2
      return 1
      ;;
  esac

  if ! "${tool}" "$@"; then
    echo "error: Flox tool '${tool}' failed its verification command" >&2
    return 1
  fi
}

provision_flox_tools() {
  # Materialize the committed environment (downloads pinned store paths on a
  # fresh sandbox; no-op when already realized) and put its bin dir on PATH
  # for the rest of this script.
  # --mode run everywhere: flox refuses to activate an env in dev mode while
  # another shell (e.g. an agent's) holds a run-mode activation of the same env.
  if ! flox activate --dir "${REPO_ROOT}" --mode run -- true; then
    FLOX_FAILURE_STAGE="activation or materialization"
    return 1
  fi
  FLOX_BIN="${REPO_ROOT}/.flox/run/$(uname -m | sed s/arm64/aarch64/)-$(uname -s | tr '[:upper:]' '[:lower:]').gtm-sdk-run/bin"
  [[ -d ${FLOX_BIN} ]] || {
    echo "error: Flox bin directory is missing: ${FLOX_BIN}" >&2
    FLOX_FAILURE_STAGE="bin directory discovery"
    return 1
  }
  export PATH="${FLOX_BIN}:${PATH}"

  while read -r tool version_flag; do
    # shellcheck disable=SC2310 # A failed probe deliberately selects fallback provisioning.
    verify_flox_tool "${tool}" "${version_flag}" || {
      FLOX_FAILURE_STAGE="tool verification"
      return 1
    }
  done <<'EOF'
uv --version
dolt version
infisical --version
gh --version
bd version
roborev version
EOF
}

# shellcheck disable=SC2310 # A failed Flox probe deliberately selects fallback provisioning.
if command -v flox >/dev/null 2>&1 && provision_flox_tools; then
  FLOX_TOOLS_VERIFIED=1
  echo "info: provisioning source: Flox (${FLOX_BIN})"
else
  if command -v flox >/dev/null 2>&1; then
    echo "warning: Flox ${FLOX_FAILURE_STAGE:-provisioning} failed; using fallback installers"
    [[ -n ${FLOX_BIN:-} ]] && PATH="${PATH#"${FLOX_BIN}:"}"
  fi
  echo "info: provisioning source: fallback installers"
  # Non-Flox fallback (macOS Conductor workspaces): original installers.
  if ! command -v dolt >/dev/null 2>&1; then
    sudo bash -c 'curl -L https://github.com/dolthub/dolt/releases/latest/download/install.sh | bash'
  fi
  if command -v dnf >/dev/null 2>&1 && ! command -v infisical >/dev/null 2>&1; then
    curl -1sLf 'https://artifacts-cli.infisical.com/setup.rpm.sh' | sudo -E bash
    sudo dnf install -y infisical
  fi
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${PATH}"
  fi
fi
if [[ ${FLOX_TOOLS_VERIFIED:-0} != 1 ]]; then
  dolt version
  uv --version
fi

# --- bd + roborev ------------------------------------------------------------
# Flox-managed on Linux sandboxes via each tool's own flake.nix (pinned in
# .flox/env/manifest.toml as bd.flake / roborev.flake), already on PATH from
# the flox activate above. macOS (no unattended Flox install) falls back to
# curl-installing the unpinned latest release, same as dolt/infisical/uv above.
if ! command -v bd >/dev/null 2>&1; then
  echo "info: installing bd with fallback installer"
  curl -fsSL https://raw.githubusercontent.com/gastownhall/beads/main/scripts/install.sh | bash
fi
if [[ ${FLOX_TOOLS_VERIFIED:-0} != 1 ]]; then
  bd version
fi

if ! command -v roborev >/dev/null 2>&1; then
  echo "info: installing roborev with fallback installer"
  curl -fsSL https://roborev.io/install.sh | bash
fi
if [[ ${FLOX_TOOLS_VERIFIED:-0} != 1 ]]; then
  roborev version
fi
git config --global alias.roborev '!roborev'

# --- Beads DB bootstrap ------------------------------------------------------
# Standalone cloud sandboxes have no parent ai/ repo, so there is no shared
# .beads to symlink to. Prefer it when it's a real Dolt DB (existence alone
# isn't enough: bogus dirs like a stray $HOME/.beads from a global bd install
# pass the -e check but aren't a real project), otherwise seed a fresh local
# DB from the shared DoltHub remote so the sandbox sees real issue history.
if [[ ! -e .beads ]] && [[ -e "${PRIMARY_REPO_ROOT}/.beads" ]] && bd -C "${PRIMARY_REPO_ROOT}" status >/dev/null 2>&1; then
  BEADS_REAL="$(cd "${PRIMARY_REPO_ROOT}/.beads" && pwd -P)"
  ln -s "${BEADS_REAL}" .beads
fi

if [[ ! -e .beads ]]; then
  DOLT_REMOTE_URL="https://doltremoteapi.dolthub.com/elviskahoro/gtm-sdk"
  if [[ -f .env.local ]]; then
    set -a && source .env.local && set +a
  fi
  DOLTHUB_API_KEY="${DOLTHUB_API_KEY-}"
  if command -v infisical >/dev/null 2>&1 && [[ -n ${INFISICAL_TOKEN-} ]] && [[ -n ${INFISICAL_PROJECT_ID-} ]]; then
    DOLTHUB_API_KEY="$(infisical secrets get DOLTHUB_API_KEY --projectId "${INFISICAL_PROJECT_ID}" --token "${INFISICAL_TOKEN}" --env=dev --plain 2>/dev/null || true)"
  fi
  if [[ -n ${DOLTHUB_API_KEY} ]]; then
    DOLT_REMOTE_USER="elviskahoro" DOLT_REMOTE_PASSWORD="${DOLTHUB_API_KEY}" \
      bd init --non-interactive --remote "${DOLT_REMOTE_URL}" ||
      echo "warning: could not seed beads DB from ${DOLT_REMOTE_URL}, falling back to a fresh local database"
  fi
  [[ ! -e .beads ]] && bd init --non-interactive --init-if-missing
fi

# --- Python project ----------------------------------------------------------
git submodule update --init --recursive
uv sync
