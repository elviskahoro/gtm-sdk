import typer

from cli.accounts.accounts import map_account_hierarchy_command
from cli.accounts.batch import batch_add_companies_command, batch_add_people_command
from cli.accounts.people import find_people_command
from cli.accounts.research import enrich_command, research_command

app = typer.Typer(
    help=(
        "GTM workflow commands.\n\n"
        "Contract:\n"
        "- Success data is always JSON on stdout.\n"
        "- Errors are always printed to stderr.\n"
        "- Mutating commands require --apply; default is preview/no-op."
    ),
)

app.command("research")(research_command)
app.command("enrich")(enrich_command)
app.command("find-people")(find_people_command)
app.command("map-account-hierarchy")(map_account_hierarchy_command)
app.command("batch-add-people")(batch_add_people_command)
app.command("batch-add-companies")(batch_add_companies_command)
