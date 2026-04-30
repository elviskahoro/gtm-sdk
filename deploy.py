# Modal deployment entrypoint — run: modal deploy deploy.py
# Must be at project root so src/attio/ doesn't shadow the attio pip package.
from src.app import app  # noqa: F401

_ = app
