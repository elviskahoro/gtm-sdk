"""Tests for scripts/downstream-contract-sync.py's import scanner.

The scanner decides what counts as gtm-sdk public API, so a false negative here
silently removes a symbol's protection and a false positive pins surface this
repo never owned. Both mistakes are invisible in the generated file — it just
looks slightly different — which is why they get direct tests.

Two failure modes are covered specifically, both caught in review:

1. ``src`` and ``cli`` are ambiguous. Consumers routinely have their own
   top-level ``src/`` and ``cli/`` (crm-uploader and dlt-hub/gtm-os both do), so
   root-name matching alone records consumer-local modules as SDK dependencies.
2. ``import libs.attio.people as people`` names no symbols on the import node,
   so a naive scan pins the module and leaves every symbol reached through the
   alias unprotected.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "downstream-contract-sync.py"
)


def _load_script_module() -> ModuleType:
    """Import the hyphenated script by path, as the sibling script tests do."""
    spec = importlib.util.spec_from_file_location(
        "downstream_contract_sync",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load_script_module()


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


@pytest.fixture
def consumer(tmp_path: Path) -> Path:
    root = tmp_path / "fake-consumer"
    root.mkdir()
    return root


def test_records_from_imports(consumer: Path) -> None:
    _write(
        consumer,
        "app/loader.py",
        "from libs.attio.companies import find_company_by_domain\n",
    )

    assert sync.scan_consumer(consumer) == {
        "libs.attio.companies": {"find_company_by_domain"},
    }


def test_records_function_local_imports(consumer: Path) -> None:
    """Deferred imports are the reason this scanner uses ast and not grep."""
    _write(
        consumer,
        "app/loader.py",
        "def run() -> None:\n"
        "    from libs.attio.people import find_person_by_name_at_company\n"
        "\n"
        "    find_person_by_name_at_company()\n",
    )

    assert sync.scan_consumer(consumer) == {
        "libs.attio.people": {"find_person_by_name_at_company"},
    }


def test_ignores_consumer_local_src_package(consumer: Path) -> None:
    """A consumer's own `src/` must not be recorded as gtm-sdk surface.

    `src` is in SDK_ROOTS because this repo publishes it, but the consumer owns
    a `src/` too, and at runtime its own package shadows ours. Recording
    `src.tam.workflow` here would pin a module this repo has never had.
    """
    _write(consumer, "src/tam/workflow.py", "")
    _write(
        consumer,
        "app/entry.py",
        "from src.tam.workflow import run_tam\nfrom libs.attio import get_client\n",
    )

    assert sync.scan_consumer(consumer) == {"libs.attio": {"get_client"}}


def test_ignores_modules_absent_from_this_repo(consumer: Path) -> None:
    """A `libs.*` import that does not exist here is the consumer's own."""
    _write(
        consumer,
        "app/entry.py",
        "from libs.not_a_real_adapter import thing\nfrom libs.attio import get_client\n",
    )

    assert sync.scan_consumer(consumer) == {"libs.attio": {"get_client"}}


def test_recovers_symbols_used_through_a_module_alias(consumer: Path) -> None:
    """`import X as y` names no symbols; `y.symbol` usage must still be pinned."""
    _write(
        consumer,
        "app/entry.py",
        "import libs.attio.people as people\n"
        "\n"
        "def run() -> None:\n"
        "    people.upsert_person(None)\n"
        "    people.search_people(None)\n",
    )

    assert sync.scan_consumer(consumer) == {
        "libs.attio.people": {"upsert_person", "search_people"},
    }


def test_recovers_symbols_used_through_a_from_imported_submodule(
    consumer: Path,
) -> None:
    """`from libs.attio import people` binds a submodule, not a callable."""
    _write(
        consumer,
        "app/entry.py",
        "from libs.attio import people\n"
        "\n"
        "def run() -> None:\n"
        "    people.stub_create_person()\n",
    )

    scanned = sync.scan_consumer(consumer)
    # The submodule name itself is consumed from the package...
    assert "people" in scanned["libs.attio"]
    # ...and the symbol reached through it is pinned on the submodule.
    assert scanned["libs.attio.people"] == {"stub_create_person"}


def test_bare_import_pins_the_module_without_symbols(consumer: Path) -> None:
    _write(consumer, "app/entry.py", "import libs.attio.notes\n")

    assert sync.scan_consumer(consumer) == {"libs.attio.notes": set()}


def test_ignores_relative_imports(consumer: Path) -> None:
    _write(consumer, "app/__init__.py", "")
    _write(consumer, "app/helpers.py", "")
    _write(
        consumer,
        "app/entry.py",
        "from .helpers import thing\nfrom libs.attio import get_client\n",
    )

    assert sync.scan_consumer(consumer) == {"libs.attio": {"get_client"}}


def test_skips_vendored_trees(consumer: Path) -> None:
    """A consumer's .venv contains gtm-sdk itself; scanning it is meaningless."""
    _write(
        consumer,
        ".venv/lib/python3.13/site-packages/whatever.py",
        "from libs.attio import upsert_company\n",
    )
    _write(consumer, "app/entry.py", "from libs.attio import get_client\n")

    assert sync.scan_consumer(consumer) == {"libs.attio": {"get_client"}}


def test_unparseable_file_does_not_truncate_the_scan(
    consumer: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(consumer, "app/broken.py", "def (:\n")
    _write(consumer, "app/entry.py", "from libs.attio import get_client\n")

    assert sync.scan_consumer(consumer) == {"libs.attio": {"get_client"}}
    assert "skipping unparseable" in capsys.readouterr().err


def test_raises_when_no_sdk_imports_are_found(consumer: Path) -> None:
    """A wrong path must fail loudly, not emit an empty contract.

    An empty scan silently disables the guard for that consumer, so it is an
    error rather than a no-op.
    """
    _write(consumer, "app/entry.py", "import os\n")

    with pytest.raises(sync.ConsumerScanError, match="no gtm-sdk imports"):
        sync.scan_consumer(consumer)


def test_raises_when_the_checkout_is_missing(tmp_path: Path) -> None:
    with pytest.raises(sync.ConsumerScanError, match="not a directory"):
        sync.scan_consumer(tmp_path / "nope")


def test_rendered_arrays_respect_the_formatter_width() -> None:
    """Rendering must match trunk's TOML formatter or the file never settles.

    If the generator wraps at a different column than `trunk fmt`, formatting
    rewrites the generated file and the sync check reports the contract as
    permanently stale.
    """
    long_symbols = {f"symbol_number_{index:02d}" for index in range(12)}
    rendered = sync.render_contract(
        {"demo": {"libs.attio": long_symbols, "libs.attio.notes": {"create_note"}}},
        {"demo": "demo consumer"},
    )

    body_lines = [
        line
        for line in rendered.splitlines()
        if line and not line.startswith(("#", "[", "description"))
    ]
    assert all(len(line) <= sync.TOML_FORMATTER_WIDTH for line in body_lines), (
        "generated line exceeds the formatter's wrap column"
    )
    # The short entry stays inline; the long one is exploded one-per-line.
    assert '"libs.attio.notes" = ["create_note"]' in rendered
    assert '"libs.attio" = [\n' in rendered
