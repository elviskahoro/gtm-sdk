import typer

from cli.gmail.url import app as url_app

app = typer.Typer(help="Gmail commands.")

app.add_typer(url_app, name="url")
