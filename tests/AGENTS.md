# Tests

Rules for working in `tests/`. `CLAUDE.md` and `WARP.md` symlink here.

`uv run pytest`. Importlib mode is already configured. Mirror the source layout
when adding tests.

## Adding a pytest plugin

The unit CI container runs with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, so a plugin
that is merely installed is **silently inert**. It has to be named explicitly,
and *where* you name it is load-bearing: put the `-p` flag in `addopts`
(`pyproject.toml`), not in `PYTEST_CMD`, and use the plugin's **entry-point
name**, not its module name.

Both traps, and why they bite, are documented at the point of use —
`pyproject.toml`'s `[tool.pytest.ini_options]` block, directly above `addopts`.
Read that before changing the flag. `tests/test_hypothesis_plugin.py` fails
loudly if the plugin stops loading, in any environment.

## Property-based tests (Hypothesis)

Files are named `test_<module>_properties.py` and sit beside the example tests
they complement — they do not replace them. Profiles live in `conftest.py`,
selected with `HYPOTHESIS_PROFILE` (see its comments for why that env var is
ours and not `CI`, and why the CI profile derandomizes).

```shell
uv run pytest --hypothesis-show-statistics    # dev: 50 examples, 400ms deadline
HYPOTHESIS_PROFILE=nightly uv run pytest      # 1000 examples, randomized
```

Reach for a property when the claim is *universal* ("never raises",
"idempotent", "round-trips", "output is always lowercase"). Two authoring rules
that live nowhere else:

- **Finite, enumerable domain → check it exhaustively with a loop, not
  `st.sampled_from`.** `max_examples` caps the draws below the domain size, and
  under `derandomize=True` the same subset is drawn forever, leaving the rest
  permanently untested. Hypothesis earns its place on *unbounded* domains.
- **Watch for vacuous properties.** If the interesting branch only runs on a
  successful parse, bare `st.text()` will miss it nearly every time. Generate
  realistic inputs, and use `hypothesis.event()` so the branch split appears in
  `--hypothesis-show-statistics` instead of being assumed.
