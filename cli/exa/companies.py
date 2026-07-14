"""Exa find-companies CLI command."""

import json
import sys

import modal
import typer
from pydantic import ValidationError

from cli.json_validation import emit_json_payload_validation_telemetry
from src.exa.companies import FindCompaniesQuery
from src.modal_app import MODAL_APP

app = typer.Typer(help="Find companies via Exa.")


@app.command(name="find-companies")
def find_companies(
    query: str = typer.Argument(
        "",
        help="Company search query (required unless --json used)",
    ),
    num_results: int = typer.Option(5, "--num-results", help="Number of results"),
    highlights: bool = typer.Option(
        True,
        "--highlights/--no-highlights",
        help="Include highlight snippets in each result",
    ),
    output_schema_json: str | None = typer.Option(
        None,
        "--output-schema-json",
        help="JSON schema for structured output",
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
    """Find companies by query via Exa."""
    if json_input:
        try:
            query_obj = FindCompaniesQuery.model_validate_json(json_input)
            payload = query_obj.model_dump(exclude_none=True)
        except (ValidationError, json.JSONDecodeError) as e:
            emit_json_payload_validation_telemetry(
                "exa.find_companies",
                e,
                FindCompaniesQuery,
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
        if output_schema_json:
            try:
                payload["output_schema"] = json.loads(output_schema_json)
            except json.JSONDecodeError as e:
                print(f"Error: invalid output_schema_json: {e}", file=sys.stderr)
                raise typer.Exit(code=1)

    api_keys = {"exa_api_key": exa_api_key_override} if exa_api_key_override else {}
    try:
        fn = modal.Function.from_name(
            MODAL_APP,
            "exa_find_companies",
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
