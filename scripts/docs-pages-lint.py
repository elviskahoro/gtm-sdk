#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Lint docs/ pages for the conventions the Mintlify site depends on.

Checks every page under docs/ (``.mdx``, plus any legacy ``.md``):

1. Frontmatter has a ``title`` and a ``description``.
2. The description is a single line under 300 characters — Mintlify serves it
   as the page's llms.txt entry, and llms.txt truncates at 300 chars / first
   line break, so a violation silently degrades agent-facing output.
3. The body contains no ``#`` H1 heading — Mintlify renders the frontmatter
   title as the H1, so a body H1 duplicates it on the rendered page.
4. Adapter-inventory completeness: every ``libs/<dir>`` adapter appears in
   ``docs/sdk/index.mdx`` (skipped until that page exists). This is the
   guard against the README-adapter-table rot happening again — the docs
   adapter index must never silently lag ``ls libs/``.

Runs standalone (stdlib only, no repo imports) so CI can call it before
``uv sync`` finishes if needed:

    uv run scripts/docs-pages-lint.py

Exit code 0 = clean, 1 = findings (printed one per line to stderr).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
LIBS_DIR = REPO_ROOT / "libs"
SDK_INDEX = DOCS_DIR / "sdk" / "index.mdx"

# Directories under docs/ that hold non-page files: snippets are MDX fragments
# imported by pages (no frontmatter of their own), styles is Vale config.
NON_PAGE_DIRS = {"snippets", "styles", "logo"}

MAX_DESCRIPTION_LEN = 300

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_FIELD_RES = {
    "title": re.compile(r'^title:\s*"?(.+?)"?\s*$', re.MULTILINE),
    "description": re.compile(r'^description:\s*"?(.+?)"?\s*$', re.MULTILINE),
}


def _iter_pages() -> list[Path]:
    pages: list[Path] = []
    for path in sorted(DOCS_DIR.rglob("*")):
        if path.suffix not in {".mdx", ".md"}:
            continue
        rel = path.relative_to(DOCS_DIR)
        if rel.parts and rel.parts[0] in NON_PAGE_DIRS:
            continue
        pages.append(path)
    return pages


def _check_page(path: Path) -> list[str]:
    rel = path.relative_to(REPO_ROOT)
    text = path.read_text(encoding="utf-8")
    findings: list[str] = []

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return [f"{rel}: missing frontmatter block"]

    frontmatter = match.group(1)
    for field, field_re in _FIELD_RES.items():
        field_match = field_re.search(frontmatter)
        if not field_match:
            findings.append(f"{rel}: frontmatter missing '{field}'")
            continue
        if field == "description":
            value = field_match.group(1).strip()
            if len(value) > MAX_DESCRIPTION_LEN:
                findings.append(
                    f"{rel}: description is {len(value)} chars "
                    f"(max {MAX_DESCRIPTION_LEN} — llms.txt truncates beyond it)",
                )

    body = text[match.end() :]
    in_fence = False
    for lineno, line in enumerate(
        body.splitlines(),
        start=match.group(0).count("\n") + 1,
    ):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^#\s", line):
            findings.append(
                f"{rel}:{lineno}: body H1 ('# ...') — Mintlify renders the "
                "frontmatter title as H1; start body headings at ##",
            )
    return findings


def _check_adapter_inventory() -> list[str]:
    if not SDK_INDEX.exists():
        return []
    index_text = SDK_INDEX.read_text(encoding="utf-8")
    findings: list[str] = []
    for entry in sorted(LIBS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("__"):
            continue
        if (
            f"`{entry.name}`" not in index_text
            and f"libs/{entry.name}" not in index_text
        ):
            findings.append(
                f"docs/sdk/index.mdx: adapter 'libs/{entry.name}' is not listed — "
                "the adapter index must cover every libs/ directory",
            )
    return findings


def main() -> int:
    findings: list[str] = []
    for page in _iter_pages():
        findings.extend(_check_page(page))
    findings.extend(_check_adapter_inventory())

    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        print(f"\n{len(findings)} docs lint finding(s).", file=sys.stderr)
        return 1
    print(f"docs-pages-lint: {len(_iter_pages())} pages clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
