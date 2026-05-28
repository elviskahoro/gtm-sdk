"""Exa CLI subapp."""

import typer

from cli.exa.companies import app as companies_app
from cli.exa.people import app as people_app
from cli.exa.search import app as search_app

app: typer.Typer = typer.Typer(help="Search the web via Exa.")

app.add_typer(search_app)
app.add_typer(companies_app)
app.add_typer(people_app)

__all__ = ["app"]
