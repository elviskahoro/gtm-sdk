# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Minimal Bzlmod bootstrap (plan ai-m4p9, Task 3): Bazel 8.7.0 is pinned in
  `.bazelversion`, and `MODULE.bazel` declares `rules_python` 2.2.0 plus a
  hermetic Python 3.13.13 toolchain. Third-party wheels resolve from
  `requirements_bazel.txt`, the frozen projection of `uv.lock`, so Bazel's
  pip layer stays hash-pinned and never re-resolves at build time. The module
  lock (`MODULE.bazel.lock`) enforces cleanly via `--lockfile_mode=error`. No
  first-party application or test targets, and no remote cache or remote
  execution, are introduced.
