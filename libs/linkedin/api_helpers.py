"""Helper functions for LinkedIn API interactions."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import logfire as logging
import orjson
from dlt.sources.helpers import requests
from requests.exceptions import HTTPError

from libs.linkedin.member_data import Empty

if TYPE_CHECKING:
    from collections.abc import Generator

    from requests import Response

HTTP_TOO_MANY_REQUESTS: int = 429


def testing_load_data() -> dict[str, Any]:
    """Load test data from local JSON file."""
    data_file: Path = Path(__file__).parent / "data" / "linkedin-response.json"
    if not data_file.exists():
        error_msg: str = f"Test data file not found: {data_file}"
        raise ValueError(error_msg)

    logging.info(f"Using test data from {data_file}")
    response_json: dict[str, Any] = orjson.loads(data_file.read_bytes())
    logging.info(f"Test data response: {response_json}")
    return response_json


def get_api_token() -> str:
    """Get and validate LinkedIn API token from environment."""
    token: str | None = os.getenv("LINKEDIN_API_KEY")
    if not token:
        error_msg: str = "LINKEDIN_API_KEY environment variable is not set"
        raise ValueError(error_msg)
    return token


def _log_rate_limit_headers(response: Response) -> None:
    """Log rate limit headers from response."""
    logging.error("Rate limit exceeded!")
    logging.error(
        "Headers: {headers}",
        headers=dict(response.headers),
    )

    if "Retry-After" in response.headers:
        logging.error(
            "Retry-After: {retry}",
            retry=response.headers["Retry-After"],
        )
    if "X-RateLimit-Reset" in response.headers:
        logging.error(
            "X-RateLimit-Reset: {reset}",
            reset=response.headers["X-RateLimit-Reset"],
        )
    if "X-RateLimit-Limit" in response.headers:
        logging.error(
            "X-RateLimit-Limit: {limit}",
            limit=response.headers["X-RateLimit-Limit"],
        )
    if "X-RateLimit-Remaining" in response.headers:
        logging.error(
            "X-RateLimit-Remaining: {remaining}",
            remaining=response.headers["X-RateLimit-Remaining"],
        )


def make_linkedin_request(
    url: str,
    headers: dict[str, str],
    params: dict[str, str | int],
) -> Response:
    """Make LinkedIn API request with error handling."""
    try:
        response: Response = requests.get(
            url,
            headers=headers,
            params=params,
        )

    except HTTPError as e:
        response: Response = e.response
        logging.error("HTTP Error occurred!")
        logging.error(
            "Response Status Code: {code}",
            code=response.status_code,
        )
        try:
            response_json: dict[str, Any] = response.json()
            logging.info(
                "{json}",
                json=response_json,
            )

        except (ValueError, TypeError) as json_error:
            logging.error(
                "Failed to parse response JSON: {error}",
                error=str(json_error),
            )
            logging.error(
                "Raw response text: {text}",
                text=response.text,
            )

        # Check for rate limiting
        if response.status_code == HTTP_TOO_MANY_REQUESTS:
            _log_rate_limit_headers(response)

        # Re-raise the original exception
        raise

    return response


def save_response_for_debug(response_json: dict[str, Any]) -> None:
    """Save response to filesystem for debugging."""
    data_dir: Path = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)
    timestamp: str = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M")
    response_file: Path = data_dir / f"linkedin-response-{timestamp}.json"
    response_file.write_bytes(
        orjson.dumps(
            response_json,
            option=orjson.OPT_INDENT_2,
        ),
    )
    logging.info(f"Saved response to {response_file}")


def parse_and_yield_elements(
    response_json: dict[str, Any],
) -> Generator[dict[str, Any]]:
    """Parse response JSON and yield individual elements."""
    parsed_response: Empty = Empty.model_validate(obj=response_json)
    for element in parsed_response.elements:
        yield element.model_dump()
