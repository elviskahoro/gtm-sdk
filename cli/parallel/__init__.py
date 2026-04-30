import typer

from cli.parallel.extract import app as extract_app
from cli.parallel.findall import app as findall_app
from cli.parallel.search import app as search_app

app = typer.Typer(help="Parallel API commands.")

app.add_typer(extract_app, name="extract")
app.add_typer(findall_app, name="findall")
app.add_typer(search_app, name="search")
