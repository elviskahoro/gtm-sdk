"""Exa find-people CLI command."""

import json
import sys

import modal
import typer
from pydantic import ValidationError

from cli.json_validation import emit_json_payload_validation_telemetry
from src.exa.people import FindPeopleQuery
from src.modal_app import MODAL_APP

app = typer.Typer(help="Find people via Exa.")


@app.command(name="find-people")
def find_people(
    query: str = typer.Argument(
        "",
        help="Person search query (required unless --json used)",
    ),
    num_results: int = typer.Option(5, "--num-results", help="Number of results"),
    highlights: bool = typer.Option(
        True,
        "--highlights/--no-highlights",
        help="Include highlight snippets in each result",
    ),
    json_input: str | None = typer.Option(
        None,
        "--json",
        help="Full JSON payload (overrides flags)",
    ),
    exa_api_key_override: str | None = typer.Option(
        None,
        "--exa-api-key",
        help="Override the Exa API key for this invocation",
    ),
) -> None:
    """Find people by query via Exa."""
    if json_input:
        try:
            query_obj = FindPeopleQuery.model_validate_json(json_input)
            payload = query_obj.model_dump(exclude_none=True)
        except (ValidationError, json.JSONDecodeError) as e:
            emit_json_payload_validation_telemetry(
                "exa.find_people",
                e,
                FindPeopleQuery,
                json_input,
            )
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(code=1)
    else:
        if not query:
            print("Error: query is required when --json is not used", file=sys.stderr)
            raise typer.Exit(code=1)

        payload = {
            "query": query,
            "num_results": num_results,
            "include_highlights": highlights,
        }

    api_keys = {"exa_api_key": exa_api_key_override} if exa_api_key_override else {}
    try:
        fn = modal.Function.from_name(
            MODAL_APP,
            "exa_find_people",
        )  # pyrefly: ignore[invalid-param-spec]
        result = fn.remote(
            payload=payload,
            api_keys=api_keys or None,
        )  # pyrefly: ignore[invalid-param-spec]  # pyright: ignore[reportFunctionMemberAccess,reportUnknownMemberType]
        out = (
            result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        )
        print(json.dumps(out, indent=2))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(code=1)
