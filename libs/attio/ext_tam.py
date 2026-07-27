"""Partner-agnostic read helpers for the Attio ``ext_tam`` custom object.

No Snowflake-specific or dltHub-specific names should appear here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from libs.attio.client import get_client

EXT_TAM_OBJECT = "ext_tam"


def iter_company_ids_by_filter(
    filter_: dict[str, Any],
    *,
    page_size: int = 100,
) -> Iterator[str]:
    """Page through ext_tam records matching ``filter_``; yield unique Company ids.

    Yields the ``accounts[0].target_record_id`` (primary account Company id) for
    each ext_tam record matching the filter. Generic — caller picks the filter
    (e.g. ``{"source": "snowflake_scored_accounts_csv"}`` or compound ``$and``
    for source + snapshot date).

    Deduplicates across pages to avoid yielding the same Company id twice.
    Skips records missing ``accounts`` or with empty ``accounts`` list.

    Args:
        filter_: Attio query filter dict (passed verbatim to the API).
        page_size: Records per page (default 100, max 100).

    Yields:
        Unique Company record_ids from accounts[0] of matching ext_tam records.

    Raises:
        ValueError: If ``page_size`` is outside 1..100 (Attio's query cap).
            ``page_size <= 0`` would otherwise prevent ``offset`` from advancing
            and loop forever; ``> 100`` exceeds Attio's per-query limit.
    """
    if not (1 <= page_size <= 100):
        raise ValueError(
            f"page_size must be between 1 and 100 inclusive; got {page_size}",
        )

    seen: set[str] = set()
    offset = 0

    with get_client() as client:
        while True:
            response = client.records.post_v2_objects_object_records_query(
                object=EXT_TAM_OBJECT,
                filter_=filter_,
                limit=page_size,
                offset=offset,
            )

            if not response.data:
                break

            for record in response.data:
                # Extract accounts[0] if present
                accounts = record.values.get("accounts", [])
                if not accounts:
                    # Skip records without accounts
                    continue

                first_account = accounts[0]
                # Attio SDK responses surface relationship values as objects
                # with attributes; the dict fallback covers the (rare) case
                # where a caller passes a pre-parsed payload through tests.
                company_id = getattr(first_account, "target_record_id", None)
                if company_id is None and hasattr(first_account, "get"):
                    company_id = first_account.get("target_record_id")
                if company_id and company_id not in seen:
                    seen.add(company_id)
                    yield company_id

            # Check if we've reached the end
            if len(response.data) < page_size:
                break

            offset += page_size
