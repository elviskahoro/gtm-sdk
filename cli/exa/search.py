"""Exa search CLI command."""

from __future__ import annotations

import json
import sys
from typing import Any

import modal
import typer
from pydantic import ValidationError

from cli.json_validation import emit_json_payload_validation_telemetry
from src.exa.search import SearchQuery
from src.modal_app import MODAL_APP

app = typer.Typer(help="Run a generic Exa search.")


@app.command(name="search")
def search(
    query: str = typer.Argument("", help="Search query (required unless --json used)"),
    type_: str = typer.Option(
        "auto",
        "--type",
        help="Search type: auto, fast, instant, deep-lite, deep, deep-reasoning",
    ),
    category: str | None = typer.Option(
        None,
        "--category",
        help="Search category (e.g. company, people)",
    ),
    num_results: int = typer.Option(
        10,
        "--num-results",
        help="Number of results (1-100)",
    ),
    include_domains: str | None = typer.Option(
        None,
        "--include-domains",
        help="Comma-separated domains to include",
    ),
    exclude_domains: str | None = typer.Option(
        None,
        "--exclude-domains",
        help="Comma-separated domains to exclude",
    ),
    highlights: bool = typer.Option(
        True,
        "--highlights/--no-highlights",
        help="Include highlight snippets in each result",
    ),
    summary: bool = typer.Option(
        False,
        "--summary/--no-summary",
        help="Include an LLM-generated summary per result",
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
    """Search the web via Exa."""
    if json_input:
        try:
            query_obj = SearchQuery.model_validate_json(json_input)
            payload = query_obj.model_dump(exclude_none=True)
        except (ValidationError, json.JSONDecodeError) as e:
            emit_json_payload_validation_telemetry(
                "exa.search",
                e,
                SearchQuery,
                json_input,
            )
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(code=1)
    else:
        if not query:
            print("Error: query is required when --json is not used", file=sys.stderr)
            raise typer.Exit(code=1)

        payload: dict[str, Any] = {
            "query": query,
            "type": type_,
            "num_results": num_results,
        }
        if category:
            payload["category"] = category
        # Drop empty segments so ``--include-domains "a,,b"`` or a trailing
        # comma doesn't send an empty domain to Exa. An all-blank list
        # (``--include-domains ","``) collapses to nothing — omit the field
        # entirely so the model's "must be non-empty when set" validator
        # doesn't trip on what is effectively unset (roborev finding).
        if include_domains:
            cleaned = [d.strip() for d in include_domains.split(",") if d.strip()]
            if cleaned:
                payload["include_domains"] = cleaned
        if exclude_domains:
            cleaned = [d.strip() for d in exclude_domains.split(",") if d.strip()]
            if cleaned:
                payload["exclude_domains"] = cleaned

        contents = {}
        if highlights:
            contents["highlights"] = True
        if summary:
            contents["summary"] = True
        if contents:
            payload["contents"] = contents

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
            "exa_search",
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
