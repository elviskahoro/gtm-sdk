#!/usr/bin/env bash
set -euo pipefail
SRC="/Users/elvis/Documents/ai/.agents/skills"
DST="$(cd "$(dirname "${0}")/.." && pwd)/.agents/skills"
mkdir -p "${DST}"

# Add or refresh symlinks for every skill directory in ai/
for d in "${SRC}"/*/; do
  name="$(basename "${d}")"
  target="${DST}/${name}"
  if [[ -L ${target} ]] || [[ ! -e ${target} ]]; then
    ln -sfn "${d}" "${target}"
  fi
  # If ${target} exists as a real directory, leave it (local skill).
done

# Prune symlinks whose upstream skill is gone
shopt -s nullglob
for l in "${DST}"/*; do
  if [[ -L ${l} ]] && [[ ! -e ${l} ]]; then
    rm "${l}"
  fi
done
