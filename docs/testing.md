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

The checked-in CI workflow (`.github/workflows/ci.yml`) runs, on every push:
Python lint plus the offline suite on 3.10–3.14 (with Bubblewrap installed and
Ubuntu's AppArmor restriction on unprivileged user namespaces lifted, because
the runner is a 24.04 host); the wheel built with `tools/build_wheel.py`,
installed into an empty venv that must import, print its version and serve the
bundled canvas; the container image, run with `docker/seccomp.json`, whose
`rich doctor` must be green (its verdict under Docker's default profile is
recorded, not required); the canvas's typecheck, vitest suite and build; and
the M1 and M6 drives in Chromium against `rich serve --route none`. A tag
`v*` runs `release.yml`: the wheel, the image on ghcr.io, and a GitHub
release carrying the wheel, refused when `pyproject.toml` disagrees with the
tag. The workflow files are checked with `actionlint`; a workflow GitHub
refuses to start fails with no jobs and no log, which is how a red CI hid for
a day.
