import typer

from cli.attio import enrichment
from cli.attio.companies import app as companies_app
from cli.attio.notes import app as notes_app
from cli.attio.people import app as people_app

app = typer.Typer(help="Attio CRM commands.")

enrichment_app = typer.Typer(help="Backfill missing fields on Attio records.")
enrichment_app.command(name="backfill-domains")(enrichment.backfill_domains)

app.add_typer(people_app, name="people")
app.add_typer(companies_app, name="companies")
app.add_typer(notes_app, name="notes")
app.add_typer(enrichment_app, name="enrichment")
