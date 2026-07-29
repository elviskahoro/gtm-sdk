"""Guard: the Hypothesis pytest plugin must actually be loaded, including in CI.

The unit CI container runs with ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` and
re-enables plugins one at a time by name — see ``PYTEST_CMD`` in
``.github/workflows/ci/pytest_dagger.py``. Hypothesis's core (``@given``,
strategies, shrinking) is plain Python and needs no plugin, so dropping
``-p _hypothesis_pytestplugin`` from that allowlist would NOT fail a single
property test. It would silently remove ``--hypothesis-seed`` (leaving no way to
replay a CI failure locally), ``--hypothesis-profile``,
``--hypothesis-show-statistics``, the ``function_scoped_fixture`` health check,
the ``@given``/``@parametrize`` interaction guard, and counterexample reporting
into the JUnit report that feeds Trunk analytics.

That silence is the whole problem, so it gets a test rather than a comment.

The plugin registers its own marker from ``pytest_configure``
(``config.addinivalue_line("markers", "hypothesis: ...")``), which makes the
marker's presence a direct, mechanism-independent signal that the plugin loaded
— it works whether the plugin arrived via entry-point autoload or an explicit
``-p`` flag. This is also why ``hypothesis`` is deliberately NOT declared in
``[tool.pytest.ini_options].markers``: declaring it there would satisfy this
assertion on its own and turn the guard into a no-op.
"""

from __future__ import annotations

import pytest


def test_hypothesis_pytest_plugin_is_loaded(pytestconfig: pytest.Config) -> None:
    markers: list[str] = pytestconfig.getini("markers")
    assert any(marker.startswith("hypothesis:") for marker in markers), (
        "The Hypothesis pytest plugin is not loaded, so @given tests are running "
        "without seeds, profiles, statistics or health checks. If this fails in "
        "CI, `-p _hypothesis_pytestplugin` is missing from PYTEST_CMD in "
        ".github/workflows/ci/pytest_dagger.py."
    )
