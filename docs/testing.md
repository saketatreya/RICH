# Testing and packaging

Install the Python project and its offline test dependencies in an isolated
environment:

```bash
python -m pip install -e '.[test]'
```

The default suite is hermetic and does not require model credentials:

```bash
ruff check .
python -m pytest
```

Tests which call an external model or provider must use the `live` marker. They
are skipped by default and run only when explicitly requested:

```bash
python -m pytest --run-live
```

Each live test is responsible for skipping with a useful message when its
selected provider is unavailable. Never put credentials or placeholder
credentials in test source.

The public-runtime conformance test is live because it downloads locked
packages and Playwright Chromium, but it does not call a model:

```bash
python -m pytest --run-live \
  --basetemp=.rich/live-tests \
  tests/test_public_runtime_live.py
```

It scaffolds a new approved Next.js target and runs frozen install, browser
install, lint, typecheck, unit, production build, and browser acceptance inside
the production Bubblewrap policies. Its acceptance scenarios use approved,
data-only browser steps; a protected reporter must return the exact set of
actually passed scenario IDs for that run/task/attempt context.

Keep the base directory on a filesystem with at least 3 GiB free. The complete
workspace does not reliably fit on small RAM-backed `/tmp` mounts.

Preview migration tests use an injected database connector. Production preview
migrations lazily require `psycopg`, validate the Neon endpoint, and execute only
bounded SQL migration files through the trusted runner. No generated Node
process receives the preview database credential.

The historical OpenRouter end-to-end phase runner remains a standalone,
destructive build-artifact harness:

```bash
OPENROUTER_API_KEY=... python tests/run_tests.py
```

It writes under `build/`, `build_archive/`, and `testlog/`; it is intentionally
not part of the offline pytest suite.

The checked-in CI workflow runs Python lint plus the offline suite, then installs
the Canvas from its lockfile, audits dependencies, typechecks, and builds the
production frontend. The host-specific Bubblewrap/Chromium conformance gate stays
explicit because shared CI runners do not provide a uniform user-namespace policy.
