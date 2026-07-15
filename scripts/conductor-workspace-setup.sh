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
if command -v flox >/dev/null 2>&1; then
  # Materialize the committed environment (downloads pinned store paths on a
  # fresh sandbox; no-op when already realized) and put its bin dir on PATH
  # for the rest of this script.
  # --mode run everywhere: flox refuses to activate an env in dev mode while
  # another shell (e.g. one whose ~/.bashrc ran the line appended below) holds
  # a run-mode activation of the same env.
  flox activate --dir "${REPO_ROOT}" --mode run -- true
  FLOX_BIN="${REPO_ROOT}/.flox/run/$(uname -m | sed s/arm64/aarch64/)-$(uname -s | tr '[:upper:]' '[:lower:]').gtm-sdk-run/bin"
  [[ -d ${FLOX_BIN} ]] && export PATH="${FLOX_BIN}:${PATH}"

  # Later interactive Conductor shells get the env via ~/.bashrc.
  FLOX_ACTIVATE_LINE="eval \"\$(flox activate --dir '${REPO_ROOT}' --mode run 2>/dev/null)\" || true"
  if ! grep -qF "${FLOX_ACTIVATE_LINE}" "${HOME}/.bashrc" 2>/dev/null; then
    printf '\n# gtm-sdk conductor setup: flox env on PATH for interactive shells\n%s\n' "${FLOX_ACTIVATE_LINE}" >>"${HOME}/.bashrc"
  fi
else
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
dolt version
uv --version

# --- bd + roborev (not in the Flox catalog yet; see follow-up issue) --------
if ! command -v bd >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/gastownhall/beads/main/scripts/install.sh | bash
fi
bd version

if ! command -v roborev >/dev/null 2>&1; then
  curl -fsSL https://roborev.io/install.sh | bash
fi
roborev version
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
