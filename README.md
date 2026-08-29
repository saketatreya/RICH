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
- exact Node 22.22.3 and pnpm 10.34.5 identity checks;
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
- an immutable full-source ZIP tied to the acceptance evidence; and
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

### Run it

```bash
python -m pip install -e '.[test]'
npm --prefix web ci && npm --prefix web run build

rich doctor      # coarse host check: bubblewrap, node, pnpm, npm, git on PATH
                 # (exact Node/pnpm identity is verified when a run starts)
rich serve       # → http://127.0.0.1:8767
```

One server answers both the canvas and the JSON API under `/v1`. Mutating calls
require an `Idempotency-Key`; execution and preview stay approval- and
digest-gated.

For a model-backed run you need either `ANTHROPIC_API_KEY` or an existing
`claude` login — the two routes are chosen explicitly and are never fallbacks
for one another. Preview deployment additionally uses `NEON_API_TOKEN` and
`VERCEL_TOKEN`. Credentials resolve lazily and are never written into run
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
