"""Sanity blog download CLI command."""

from __future__ import annotations

from pathlib import Path

import typer

from libs.sanity.client import (
    DEFAULT_API_VERSION,
    DEFAULT_DATASET,
    DEFAULT_PROJECT_ID,
    SanityConfig,
    api_key_scope,
)
from src.sanity.blog_download import download_blog_posts

app = typer.Typer(help="Download blog posts from Sanity.")


@app.command(name="download")
def download(
    out_dir: Path = typer.Option(
        Path("out"),
        "--out-dir",
        help="Output directory; posts are written under <out-dir>/blogs/<slug>/.",
    ),
    project_id: str = typer.Option(DEFAULT_PROJECT_ID, "--project-id"),
    dataset: str = typer.Option(DEFAULT_DATASET, "--dataset"),
    api_version: str = typer.Option(DEFAULT_API_VERSION, "--api-version"),
    token: str | None = typer.Option(
        None,
        "--token",
        help="Bearer token for private datasets (public datasets need none).",
    ),
    use_cdn: bool = typer.Option(
        False,
        "--use-cdn/--no-cdn",
        help=(
            "Read from the cached CDN edge instead of the live origin. Off by "
            "default so an archive run never captures stale content or misses "
            "just-published/just-deleted posts."
        ),
    ),
    use_env_token: bool = typer.Option(
        False,
        "--use-env-token/--no-env-token",
        help=(
            "Off by default so a public archive run ignores any ambient "
            "SANITY_API_TOKEN and stays reproducible. Pass --use-env-token to "
            "authenticate from the environment (an explicit --token always "
            "applies regardless)."
        ),
    ),
    prune: bool = typer.Option(
        True,
        "--prune/--no-prune",
        help="Remove snapshots of posts no longer in the live corpus.",
    ),
) -> None:
    """Download every blog post to <out-dir>/blogs/<slug>/ (index.md + post.json)."""
    config = SanityConfig(
        project_id=project_id,
        dataset=dataset,
        api_version=api_version,
        use_cdn=use_cdn,
    )

    if token:
        with api_key_scope(token):
            result = download_blog_posts(
                out_dir,
                config=config,
                prune=prune,
                allow_env_token=use_env_token,
            )
    else:
        result = download_blog_posts(
            out_dir,
            config=config,
            prune=prune,
            allow_env_token=use_env_token,
        )

    typer.echo(
        f"Wrote {result.written} posts to {result.out_dir / 'blogs'} "
        f"({result.skipped} skipped, {result.pruned} pruned).",
    )
