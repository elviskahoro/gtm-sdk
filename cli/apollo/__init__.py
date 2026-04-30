import typer

from cli.apollo.organizations import app as organizations_app
from cli.apollo.people import app as people_app

app = typer.Typer(help="Apollo API commands.")

app.add_typer(people_app, name="people")
app.add_typer(organizations_app, name="organizations")
