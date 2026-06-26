"""Sanity CLI subapp."""

import typer

from cli.sanity.blog import app as blog_app

app: typer.Typer = typer.Typer(help="Download content from Sanity.")

app.add_typer(blog_app, name="blog")

__all__ = ["app"]
