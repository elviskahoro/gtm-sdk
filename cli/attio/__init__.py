import typer

from cli.attio.companies import app as companies_app
from cli.attio.notes import app as notes_app
from cli.attio.people import app as people_app

app = typer.Typer(help="Attio CRM commands.")

app.add_typer(people_app, name="people")
app.add_typer(companies_app, name="companies")
app.add_typer(notes_app, name="notes")
