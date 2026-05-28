"""Attio enrichment CLI commands."""

import json
import sys
from typing import Any

import modal
import typer
from pydantic import ValidationError

from cli.json_validation import emit_json_payload_validation_telemetry
from src.attio.enrichment import BackfillCompanyDomainsQuery
from src.modal_app import MODAL_APP


def backfill_domains(
    ext_tam_filter: str | None = typer.Option(
        None,
        "--ext-tam-filter",
        help='JSON filter for ext_tam (e.g., \'{"source":"snowflake_scored_accounts_csv"}\')',
    ),
    company_ids: str | None = typer.Option(
        None,
        "--company-ids",
        help="Comma-separated Company record IDs",
    ),
    limit: int | None = typer.Option(None, "--limit", help="Max Companies to process"),
    sleep_seconds: float = typer.Option(
        0.0,
        "--sleep-seconds",
        help="Sleep between Companies",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually PATCH (default: preview only)",
    ),
    json_input: str | None = typer.Option(
        None,
        "--json",
        help="Full JSON payload (overrides flags)",
    ),
    attio_api_key_override: str | None = typer.Option(None, "--attio-api-key"),
    exa_api_key_override: str | None = typer.Option(None, "--exa-api-key"),
) -> None:
    """Backfill missing domains on Attio Companies."""
    if json_input:
        try:
            query = BackfillCompanyDomainsQuery.model_validate_json(json_input)
            payload = query.model_dump(exclude_none=True)
        except (ValidationError, json.JSONDecodeError) as e:
            emit_json_payload_validation_telemetry(
                "attio.enrichment.backfill_domains",
                e,
                BackfillCompanyDomainsQuery,
                json_input,
            )
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(code=1)
    else:
        # Build payload from flags. Check *presence* of the flags (``is not None``)
        # rather than truthiness — otherwise ``--ext-tam-filter '{}'`` slips past
        # the mutual-exclusion guard because it's truthy-empty (roborev finding).
        # The model-level validator catches empty contents too, but rejecting
        # here gives a clearer error than a downstream ValidationError.
        payload: dict[str, Any] = {}

        filter_supplied = ext_tam_filter is not None
        ids_supplied = company_ids is not None

        if filter_supplied:
            try:
                parsed_filter = json.loads(ext_tam_filter or "")
            except json.JSONDecodeError as e:
                print(f"Error: invalid --ext-tam-filter JSON: {e}", file=sys.stderr)
                raise typer.Exit(code=1)
            if not isinstance(parsed_filter, dict) or not parsed_filter:
                print(
                    "Error: --ext-tam-filter must be a non-empty JSON object",
                    file=sys.stderr,
                )
                raise typer.Exit(code=1)
            payload["ext_tam_filter"] = parsed_filter

        if filter_supplied and ids_supplied:
            print(
                "Error: --ext-tam-filter and --company-ids are mutually exclusive",
                file=sys.stderr,
            )
            raise typer.Exit(code=1)

        if not filter_supplied and not ids_supplied:
            print(
                "Error: one of --ext-tam-filter or --company-ids is required",
                file=sys.stderr,
            )
            raise typer.Exit(code=1)

        if ids_supplied:
            payload["company_ids"] = [
                cid.strip() for cid in (company_ids or "").split(",") if cid.strip()
            ]

        if limit is not None:
            payload["limit"] = limit
        # Always forward ``sleep_seconds`` so the model rejects negatives.
        # Previously gated on ``> 0``, which silently coerced ``--sleep-seconds
        # -1`` to the default 0.0 and hid user input mistakes (roborev finding).
        payload["sleep_seconds"] = sleep_seconds
        if apply:
            payload["apply"] = True

    api_keys = {}
    if attio_api_key_override:
        api_keys["attio_api_key"] = attio_api_key_override
    if exa_api_key_override:
        api_keys["exa_api_key"] = exa_api_key_override

    try:
        fn = modal.Function.from_name(
            MODAL_APP,
            "attio_backfill_company_domains",
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
