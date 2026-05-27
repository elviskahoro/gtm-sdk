"""Core Exa search function with response mapping."""

from __future__ import annotations

from typing import Any

from .client import _get_client  # pyright: ignore[reportPrivateUsage]
from .errors import ExaError, from_http_status
from .models import (
    ContentsOptions,
    GroundingCitation,
    OutputGrounding,
    SearchInput,
    SearchOutput,
    SearchResponse,
    SearchResultItem,
)


def _contents_requested(
    contents: bool | ContentsOptions | None,
) -> bool:
    """Return True iff the caller actually asked for at least one content slot.

    - ``None`` / ``False``                                  → no contents.
    - ``True``                                              → contents (defaults).
    - ``ContentsOptions`` with at least one slot enabled    → contents.
      "Enabled" means ``True`` or a nested options object; an explicit
      ``False`` is a "this slot off" signal and does NOT count as requesting.

    Empty / all-False ``ContentsOptions`` objects are object-truthy in Python;
    treating them as "contents requested" would silently route to the more
    expensive ``search_and_contents`` path.
    """
    if contents is None or contents is False:
        return False
    if contents is True:
        return True
    for slot in ("text", "highlights", "summary"):
        value = getattr(contents, slot)
        if value is None or value is False:
            continue
        # ``True`` or a nested options object counts as a real request.
        return True
    return False


def _translate_sdk_exception(exc: BaseException) -> ExaError | None:
    """Translate an Exa SDK exception into a typed ``ExaError``.

    The Exa Python SDK surfaces HTTP failures via attribute-bearing exception
    instances (``status_code``, ``response``, etc.). We sniff the exception
    duck-style rather than importing private SDK error classes (their layout
    has drifted across versions). Returns ``None`` if the exception doesn't
    look like an HTTP error so the caller can re-raise it untranslated.

    Always consults ``exc.response`` (when present) for ``request_id``/body
    even when the status is on the exception itself — many SDKs keep status
    on the exception but the diagnostic metadata on the underlying response.
    """
    status: int | None = None
    request_id: str | None = None
    body: dict[str, Any] | None = None

    # Look for status on the exception itself first.
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            status = value
            break

    # Always check ``exc.response`` for status (as a fallback) and for
    # request_id/body (regardless of where status came from).
    response = getattr(exc, "response", None)
    if response is not None:
        if status is None:
            value = getattr(response, "status_code", None)
            if isinstance(value, int):
                status = value
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                request_id = headers.get("x-request-id")
            except Exception:  # noqa: BLE001 - headers may not behave like a dict
                request_id = None
        if hasattr(response, "json"):
            try:
                body = response.json()
            except Exception:  # noqa: BLE001 - JSON parse must not mask the original
                body = None

    if status is None:
        return None
    if request_id is None:
        request_id = getattr(exc, "request_id", None)

    return from_http_status(status, body=body, request_id=request_id)


def search(input: SearchInput, api_key: str | None = None) -> SearchResponse:
    """Execute Exa search and return typed response.

    Calls either `client.search()` or `client.search_and_contents()` depending
    on whether ``contents`` is set in the input.

    Args:
        input: SearchInput with validated query parameters.
        api_key: Optional API key (overrides contextvar/env).

    Returns:
        Typed SearchResponse with results, output (if requested), and cost.
    """
    client = _get_client(api_key)

    # Build request dict with snake_case keys
    # (Exa SDK expects snake_case for the actual call)
    request_dict = input.model_dump(exclude_none=True, by_alias=False)

    # Determine which SDK method to call. Two signals route to the
    # ``search_and_contents`` endpoint:
    #   (a) the caller asked for at least one content slot, OR
    #   (b) ``output_schema`` is set — synthesized structured output ONLY
    #       comes back through ``search_and_contents``; plain ``search``
    #       does not populate ``response.output``. Without this, callers
    #       relying on ``outputSchema`` for typed extraction (e.g. company
    #       domain resolution) silently get an empty result.
    contents_requested = _contents_requested(input.contents)
    needs_contents_endpoint = contents_requested or input.output_schema is not None
    # If contents wasn't truly requested, strip the kwarg regardless of which
    # endpoint we hit — otherwise we'd forward ``contents=False`` or an
    # all-False ``ContentsOptions`` to the SDK, which neither endpoint expects.
    if not contents_requested:
        request_dict.pop("contents", None)
    try:
        if needs_contents_endpoint:
            sdk_response = client.search_and_contents(**request_dict)
        else:
            sdk_response = client.search(**request_dict)
    except Exception as exc:
        # Translate Exa SDK HTTP failures into typed ``ExaError`` subclasses
        # so callers can branch on auth / rate-limit / server cleanly. If the
        # exception isn't HTTP-shaped, re-raise it as-is.
        translated = _translate_sdk_exception(exc)
        if translated is not None:
            raise translated from exc
        raise

    # Map SDK response objects to our Pydantic models
    results = []
    if hasattr(sdk_response, "results") and sdk_response.results:
        for r in sdk_response.results:
            result_item = SearchResultItem(
                url=getattr(r, "url", ""),
                id=getattr(r, "id", None),
                title=getattr(r, "title", None),
                published_date=getattr(r, "published_date", None),
                author=getattr(r, "author", None),
                image=getattr(r, "image", None),
                favicon=getattr(r, "favicon", None),
                text=getattr(r, "text", None),
                highlights=getattr(r, "highlights", None),
                highlight_scores=getattr(r, "highlight_scores", None),
                summary=getattr(r, "summary", None),
                subpages=getattr(r, "subpages", None),
                extras=getattr(r, "extras", None),
            )
            results.append(result_item)

    # Map output (structured output from outputSchema). Use ``is not None``
    # rather than truthiness so empty-but-valid output objects / empty
    # citation lists survive — Pydantic objects are truthy regardless of
    # whether their fields are set, but custom SDK shapes might not be
    # (roborev finding).
    output = None
    sdk_output = getattr(sdk_response, "output", None)
    if sdk_output is not None:
        # Parse grounding citations. ``grounding`` may be an empty list or
        # None; both are valid responses that should still produce an
        # ``OutputGrounding`` (with empty citations) so callers can rely on
        # the shape.
        citations: list[GroundingCitation] = []
        sdk_grounding = getattr(sdk_output, "grounding", None)
        if sdk_grounding is not None:
            for cit in sdk_grounding:
                citation = GroundingCitation(
                    url=getattr(cit, "url", ""),
                    title=getattr(cit, "title", None),
                    published_date=getattr(cit, "published_date", None),
                    author=getattr(cit, "author", None),
                    text=getattr(cit, "text", None),
                    confidence=getattr(cit, "confidence", None),
                )
                citations.append(citation)

        grounding = OutputGrounding(citations=citations)

        # Content is whatever the caller's ``output_schema`` declared — any
        # JSON value (string, dict, list, number, boolean). Pass it through
        # without narrowing so ``output_schema={"type":"array",...}`` and
        # other non-string/dict shapes survive the adapter.
        content = getattr(sdk_output, "content", None)
        output = SearchOutput(content=content, grounding=grounding)

    # Extract cost information
    cost_dollars = 0.0
    if hasattr(sdk_response, "cost_dollars") and sdk_response.cost_dollars:
        cost_obj = sdk_response.cost_dollars
        if isinstance(cost_obj, dict):
            cost_dollars = float(cost_obj.get("total", 0.0))
        elif isinstance(cost_obj, (int, float)):
            cost_dollars = float(cost_obj)
        elif hasattr(cost_obj, "total"):
            cost_dollars = float(cost_obj.total)
        else:
            # Fallback: try to get the float value
            try:
                cost_dollars = float(cost_obj)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                cost_dollars = 0.0

    return SearchResponse(
        request_id=getattr(sdk_response, "request_id", None),
        search_type=getattr(sdk_response, "search_type", None),
        results=results,
        output=output,
        cost_dollars=cost_dollars,
    )
