"""Exa API type aliases for Pydantic validation."""

from typing import Literal

SearchType = Literal["auto", "fast", "instant", "deep-lite", "deep", "deep-reasoning"]
Category = Literal["general", "company", "research", "news", "twitter", "people"]
Verbosity = Literal["on", "off"]
Confidence = Literal["low", "medium", "high"]
