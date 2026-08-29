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

The product is also driven as a person would use it, in a real browser against a
running server. Each milestone's drive lives under `web/drive/` and is its acceptance
test -- it fails when the product fails, whichever layer is to blame:

```bash
PYTHONPATH=src python -m richbuild.cli serve --state-dir .rich/drive/state --port 8790 &
RICH_URL=http://127.0.0.1:8790 npm --prefix web run drive:m1
```

The delivery board is part of the tree, so its consistency is part of the suite:
`tests/test_board.py` holds `docs/board/cards/` to the rules in `tools/board.py`, and
`python tools/board.py verify` records a measured health strip at a commit.

The checked-in CI workflow runs Python lint plus the offline suite, then installs
the Canvas from its lockfile, audits dependencies, typechecks, and builds the
production frontend. The host-specific Bubblewrap/Chromium conformance gate stays
explicit because shared CI runners do not provide a uniform user-namespace policy.
