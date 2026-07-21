# Changelog

All notable changes to the gtm-sdk build system are documented here. This is a
build/infrastructure changelog; user-facing release notes live in
`docs/changelog/` on the docs site.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Bazel/Bzlmod bootstrap.** Minimal, reproducible Bazel foundation mirroring
  `ai-m4p9.15`. Introduces Bazel 8.7.0 via Bazelisk (acquired to `tmp/bin`,
  never `pip`), `rules_python` 2.2.0, and a pinned Python 3.13.13 toolchain.
  Third-party Python deps are materialized from the hashed
  `requirements_bazel.txt` (generated from `uv.lock` by
  `scripts/bazel-requirements-sync.py`) into a `@pypi` pip hub. `uv` remains
  the authoritative Python dependency manager — Bazel does not replace it.
  - `.bazelversion` pins Bazel 8.7.0.
  - `.bazelrc` enables Bzlmod, offers a local-only disk cache
    (`--config=local_cache`), and enforces the module lockfile in CI
    (`--config=ci` → `--lockfile_mode=error`). No remote cache is configured.
  - `.bazelignore` excludes tooling/generated/scratch trees (`.agents`,
    `.beads`, `.claude`, `.context`, `.dolt`, `.venv`, `data`, `data-gen`,
    `out`, `tmp`, `worktrees`) from package discovery; `webhooks/` is
    intentionally kept in scope.
  - `MODULE.bazel` declares `rules_python` 2.2.0, the Python 3.13.13
    toolchain, and the `@pypi` pip hub.
  - Root `BUILD.bazel` exports only `pyproject.toml` and
    `requirements_bazel.txt` — no first-party app/test targets yet.
  - `MODULE.bazel.lock` is committed so dependency resolution is reproducible
    and CI-enforced.

### Out of scope (this entry)

- First-party Bazel app/test targets.
- Remote caching.
- Replacing `uv`.
- Bazelifying `webhooks/`.
