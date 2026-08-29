# RICH — an intent-to-verified-software compiler

Describe what you want built. RICH interviews you into an approved
specification, designs an architecture you can revise, writes the source under a
budget, and proves it works with gates the model cannot touch — then deploys
exactly what passed.

Nothing advances on a model's say-so. Only an independently observed command
result can publish success.

The long-term goal is to generalize software development without reducing it to
“prompt → code.” RICH treats development as a compilation and evidence problem:

```text
interviewed intent
  → approved product specification
  → approved typed architecture and ownership map
  → durable dependency-ordered tasks
  → frozen target-pack scaffold
  → bounded model-authored source
  → independent lint / type / unit / build / browser gates
  → content-addressed release snapshot
  → separately approved preview deployment
```

The current vertical slice implements that chain for a production-grade Next.js
monorepo target pack. It includes:

- immutable spec and architecture revisions with explicit approval gates;
- requirement traceability plus human-approved, data-only browser oracles that
  compile into protected acceptance tests and attempt-bound coverage evidence;
- fail-closed source ownership and protected build/test/toolchain inputs;
- SQLite-backed runs, tasks, events, artifacts, idempotency, and fenced execution
  leases;
- atomic owner-token fencing for scheduler state/evidence writes plus reaped
  process-group cancellation on lease loss or deadline;
- a complete approved model/cost/time budget with crash-conservative recovery;
- CAS-backed source write-ahead transactions that recover the apply/persist crash
  window without trusting unrecorded workspace bytes;
- one exact trusted model policy (`anthropic/claude-sonnet-5`) reached by two
  explicitly chosen routes -- an API key or an existing `claude` login -- which
  are never fallbacks for one another;
- exact Node 22.23.2 and pnpm 10.34.5 identity checks;
- Bubblewrap isolation, network-off verification, bounded processes/heaps/output,
  and no unsafe local fallback;
- independent lint, TypeScript, unit, contract-obligation, production-build, and
  Playwright gates;
- proof obligations declared by a contract compiled into a runnable property
  suite against a pinned operations interface, so a claim is executed rather
  than merely stated;
- generation memoized by the exact request, so an unchanged task is not re-paid
  for -- and a single node can be marked stale and rebuilt without redoing its
  siblings;
- gate output fed back into the next attempt, redacted to what the failing task
  is allowed to read;
- cooperative durable cancellation, observable from any surface;
- an immutable full-source ZIP tied to the acceptance evidence;
- that same snapshot pushed to a Git repository as one deterministic commit
  (`rich push-repository`, `POST /v1/runs/{id}/repository-pushes`); and
- digest-bound, separately approved Neon/Vercel previews, immutable uploads,
  trusted SQL-only migrations, and teardown.

The generated source cannot modify its verifier, tests, dependency graph, or
toolchain. A successful model response is never evidence. Only independently
observed command results can publish task/run success. Evidence may flow the
other way -- a failed gate's output informs the retry -- but never back again:
evidence may inform generation, generation may never become evidence.

This is a working generalized-development **kernel**, not yet a claim that arbitrary
software is solved. The compiler/target-pack boundary is designed for more languages
and deployment shapes; the implemented pack today is Next.js, local live-workspace
execution is deliberately serialized, and distributed systems still require new
resource, migration, observability, and failure-semantics packs. See
[the architecture and operating contract](docs/architecture.md).

### Install it

```bash
python tools/build_wheel.py                 # builds the canvas, then dist/*.whl with it inside
python -m pip install dist/rich_agent_build_system-*.whl
rich doctor                                 # every host check, each failure with its remedy
rich serve                                  # → http://127.0.0.1:8767 — API and canvas, one port
```

The wheel carries the canvas, so an installed `rich serve` needs no Node
toolchain to show the product; Node 22.23.2 on `PATH` and pnpm 10.34.5 in the
Corepack cache are needed only to *build* software (`rich doctor` says exactly
what is missing and how to get it).

Releases are tags: `git tag v2.0.0 && git push --tags` builds the wheel and
the image (`ghcr.io/saketatreya/rich:2.0.0`) and publishes a GitHub release
with the wheel attached, refusing a tag whose version `pyproject.toml` does
not carry.

### Or run the image

```bash
docker build -t rich .
docker run --rm -p 127.0.0.1:8767:8767 -v rich-state:/rich \
  -e ANTHROPIC_API_KEY \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  rich
```

The image holds Bubblewrap, the pinned Node and pnpm, Chromium's system
libraries and the wheel; state and the dependency cache live on the volume.
The port is published only to the host's loopback, and inside the container
the server binds `0.0.0.0` with its Host and Origin checks enforced. Building
software needs unprivileged user namespaces inside the container, which
Docker's default seccomp profile blocks: hence the two `--security-opt`
flags, which CI proves are sufficient (and records whether the default
profile happens to allow it). `rich doctor` inside the container says either
way.

### Run it from a checkout

```bash
python -m pip install -e '.[test]'
npm --prefix web ci && npm --prefix web run build   # a checkout serves its own build

rich doctor      # host checks with remedies; exact Node/pnpm identity is verified
                 # again when a run starts
rich serve       # → http://127.0.0.1:8767
```

One server answers both the canvas and the JSON API under `/v1`. Mutating calls
require an `Idempotency-Key`; execution and preview stay approval- and
digest-gated.

For a model-backed run you need either `ANTHROPIC_API_KEY` or an existing
`claude` login — the two routes are chosen explicitly and are never fallbacks
for one another. Preview deployment additionally uses `NEON_API_TOKEN` and
`VERCEL_TOKEN`; a repository push over https uses `GITHUB_TOKEN`. Credentials resolve lazily and are never written into run
documents or model events.

The default test suite is offline:

```bash
ruff check .
python -m pytest
```

Host/toolchain conformance, including a fresh generated app through the complete
sandbox pipeline, is explicit:

```bash
python -m pytest --run-live \
  --basetemp=.rich/live-tests \
  tests/test_public_runtime_live.py
```

The generated workspace, locked dependency store, and Chromium installation can
exceed 2 GiB, so the explicit base directory avoids small RAM-backed `/tmp`
filesystems.

### What a run actually does

```text
interview            adaptive — it asks what your project raises, not a fixed form
  ↓ approve
architecture         a graph you can revise; draft, diff, apply as a new revision
  ↓ approve
compile              dependency-ordered tasks against a frozen Next.js scaffold
  ↓
generate             bounded, memoized, confined to owned paths
  ↓
verify               lint · types · unit · contract obligations · build · Playwright
  ↓ approve            all in Bubblewrap, network off, model output excluded
preview              digest-bound Neon + Vercel, torn down on request
```

Any node can be marked stale and rebuilt on its own; its siblings replay from
memo instead of being paid for twice. Any run can be told to stop at its next
checkpoint, from any surface.

### Development

```bash
ruff check .                     # lint
python -m pytest                 # the offline suite; never calls a model
npm --prefix web run typecheck
npm --prefix web run dev         # hot reload; proxies /v1 to the Python server
```

`CLAUDE.md` carries the working conventions and the invariants that must not be
weakened. `docs/architecture.md` is the operating contract — read it before
changing the engine.
