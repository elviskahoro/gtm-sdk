"""Guards for the Hypothesis setup itself: is the plugin loaded, and which
settings profile is actually in force?

Both are things CI cannot tell you by passing. The unit container runs with
``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` and re-enables plugins one at a time by
name, but Hypothesis's core (``@given``, strategies, shrinking) is plain Python
and needs no plugin — so losing the plugin would not fail a single property
test. It would silently remove ``--hypothesis-seed`` (leaving no way to replay a
CI failure locally), ``--hypothesis-profile``, ``--hypothesis-show-statistics``,
the ``function_scoped_fixture`` health check, the ``@given``/``@parametrize``
interaction guard, and counterexample reporting into the JUnit report that feeds
Trunk analytics. The profile is equally invisible: ``PYTEST_CMD`` passes neither
``-v`` nor ``--hypothesis-show-statistics``, so nothing in the CI log reports
``max_examples`` or whether generation is derandomized.

That silence is the whole problem, so both get tests rather than comments.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import settings


def test_hypothesis_pytest_plugin_is_loaded(pytestconfig: pytest.Config) -> None:
    """The plugin registers its own marker, so the marker proves it loaded.

    ``config.addinivalue_line("markers", "hypothesis: ...")`` runs in the
    plugin's ``pytest_configure``, which makes this mechanism-independent: it
    holds whether the plugin arrived via entry-point autoload or an explicit
    ``-p``. It is also why ``hypothesis`` is deliberately NOT declared in
    ``[tool.pytest.ini_options].markers`` — declaring it there would satisfy this
    assertion on its own and turn the guard into a no-op.
    """
    markers: list[str] = pytestconfig.getini("markers")
    assert any(marker.startswith("hypothesis:") for marker in markers), (
        "The Hypothesis pytest plugin is not loaded, so @given tests are running "
        "without seeds, profiles, statistics or health checks. Check that "
        "`-p hypothesispytest` is still in the active pytest config — and note "
        "it must be the entry-point name, not `-p _hypothesis_pytestplugin`, "
        "which double-registers wherever plugin autoload is enabled."
    )


# Only the knobs worth pinning: the ones the CI design argument actually rests
# on. Deliberately not a full mirror of tests/conftest.py -- that would just
# restate the registration and fail on every harmless tweak.
_EXPECTED_MAX_EXAMPLES = {"dev": 50, "ci": 200, "nightly": 1000}

# Set only by the legacy pytest CI container, never locally, which
# makes it a non-circular "am I in that container?" signal. Needed because
# HYPOTHESIS_PROFILE cannot detect its own absence -- see the test below.
_CI_CONTAINER_SIGNAL = "PYTEST_DISABLE_PLUGIN_AUTOLOAD"


def test_ci_container_actually_requests_the_ci_profile() -> None:
    """In a CI container that disables autoload, the profile must be ``ci``.

    ``test_requested_profile_is_the_one_in_force`` below cannot cover this: with
    the variable absent it validates the ``dev`` profile and passes, so a CI run
    that silently lost the variable looks identical to a healthy one. The
    workflow-source assertions catch someone editing it out of a CI entrypoint,
    but not a *new* CI entrypoint
    that never set it, nor a value that is set but wrong.

    Keyed off ``PYTEST_DISABLE_PLUGIN_AUTOLOAD`` because only that container sets
    it, so this cannot be satisfied by the very variable it is checking.
    """
    if os.environ.get(_CI_CONTAINER_SIGNAL) is None:
        pytest.skip(
            f"{_CI_CONTAINER_SIGNAL} unset — this runner does not assert a CI "
            "profile, so there "
            f"is no CI configuration to assert here",
        )
    assert os.environ.get("HYPOTHESIS_PROFILE") == "ci", (
        "running in a CI container without HYPOTHESIS_PROFILE=ci, so "
        "property tests fall back to the dev profile: a third of the examples, "
        "randomized generation, and a 400ms per-example deadline across four "
        "xdist workers. Set it in the CI container environment."
    )


def test_requested_profile_is_the_one_in_force() -> None:
    """The profile named by ``HYPOTHESIS_PROFILE`` must actually be active.

    ``HYPOTHESIS_PROFILE`` is set by the canonical Bazel Dagger command. If it
    ever stops arriving,
    the suite silently falls back to the ``dev`` profile: a third of the examples,
    randomized generation, and a 400 ms per-example deadline on a shared
    four-worker runner — i.e. exactly the Trunk-flake exposure the ``ci`` profile
    exists to remove. Nothing else in CI would report that.
    """
    profile = os.environ.get("HYPOTHESIS_PROFILE", "dev")
    # An unregistered value never reaches here -- conftest's load_profile raises
    # at import. So a miss means someone registered a new profile without
    # recording what it should do. Fail rather than skip: a silently-skipped
    # assertion is how a guard stops guarding.
    assert profile in _EXPECTED_MAX_EXAMPLES, (
        f"HYPOTHESIS_PROFILE={profile!r} is registered in tests/conftest.py but "
        f"has no expected max_examples here; add it to _EXPECTED_MAX_EXAMPLES."
    )
    expected_max_examples = _EXPECTED_MAX_EXAMPLES[profile]

    active = settings()
    assert active.max_examples == expected_max_examples, (
        f"HYPOTHESIS_PROFILE={profile!r} but max_examples={active.max_examples} "
        f"(expected {expected_max_examples}); the profile did not load."
    )

    if profile != "ci":
        return

    # Inherited from Hypothesis's built-in `ci` profile via `parent=`. Asserted
    # because they are the reason CI is safe to run property tests in at all --
    # there is no pytest-timeout, no --maxfail and no job timeout-minutes, and
    # Trunk reports any intermittent failure as a flake under a stable test ID.
    assert active.derandomize is True, (
        "the ci profile must derandomize: a randomly-seeded property test that "
        "fails once becomes a Trunk flake instead of a reproducible failure"
    )
    assert active.database is None, (
        "the ci profile must disable the example database: four "
        "--dist=loadfile xdist workers would otherwise share one directory-based "
        "DB, and the container is discarded every run regardless"
    )
    assert active.deadline is None, (
        "the ci profile must not impose a per-example deadline on a shared "
        "4-vCPU runner running four workers"
    )
