# Docs site

Rules for working in `docs/`. `CLAUDE.md` and `WARP.md` symlink here.

`docs/` is the published documentation site, so the repo-wide
"don't create summary `.md` files" rule does not apply here. Local preview:
`npm i -g mint`, then `mint dev` inside `docs/` (Node 24 via `.node-version`;
mint breaks on Node 25+).

- Every page is `.mdx` with `title` + one-line `description` frontmatter and no
  body H1 — the site renders the frontmatter title as the H1. The description
  becomes the page's llms.txt entry and is truncated at 300 chars.
  `scripts/docs-pages-lint.py` enforces all of this.
- **`docs/cli/` is generated — never hand-edit** (except `cli/index.mdx`).
  Change the `help=` strings in `cli/` and run
  `uv run scripts/docs-cli_reference-generate.py`. CI fails on drift.
- Changed a `libs/` adapter's public surface, a Modal deployment flow, or
  webhook wiring? Update the matching page in the same PR.
- Moving or renaming a page? Add a `redirects` entry in `docs.json` in the same
  PR — published URLs never die.
- **Never put personal infra here**: no real Modal URLs, Hookdeck IDs,
  Infisical project IDs, GCS bucket names, or local paths. Placeholders are
  `<UPPER_SNAKE>`.
- Vale prose-lints `docs/**` only (`.vale.ini`); new terminology needs an entry
  in `docs/styles/config/vocabularies/GtmSdk/accept.txt`.
