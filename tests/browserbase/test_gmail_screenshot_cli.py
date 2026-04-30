import sys
import types

import pytest

from archive.browserbase import gmail_screenshot_cli as cli


def test_discover_dispatches_to_archive_module(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, int] = {}

    fake_module = types.ModuleType("archive.browserbase.gmail_discover")

    def fake_run(*, limit: int = 0) -> None:
        called["limit"] = limit

    setattr(fake_module, "run", fake_run)

    monkeypatch.setitem(sys.modules, "archive.browserbase.gmail_discover", fake_module)
    monkeypatch.setattr(
        sys, "argv", ["gmail_screenshot_cli.py", "discover", "--limit", "7"]
    )

    cli.main()

    assert called == {"limit": 7}
